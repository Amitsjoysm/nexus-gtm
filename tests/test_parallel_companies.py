"""New ICP companies from Parallel: Entity Search finds, one Decisions request per company checks
(owner decisions 2026-10-29, `docs/superpowers/specs/2026-10-29-parallel-provider-design.md`).

The chain is the shared store, then LinkedIn, then Parallel, then Exa as the fallback. Every call
counts: one search asks for the 100 results its price includes, only candidates that survive the
free gates are checked, only until the request is met, and every vetted company is kept in the
shared store for the next workspace.
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from nexus.core.db import get_platform_sessionmaker
from nexus.integrations.company_search import CompanyCandidate
from nexus.integrations.parallel import Answer, DecisionOutcome, Entity, EntityOutcome
from nexus.models import Account, Company
from nexus.models.billing import BillingUsageEvent
from nexus.prospecting import companies as chain
from nexus.prospecting import parallel_companies as pc
from nexus.prospecting import vetting
from tests.conftest import make_tenant, tenant_session
from tests.test_prospect_chain import PagedClient, Web

ICP = {"industries": ["Oil & Gas"], "linkedin_industry_ids": [57],
       "countries": ["United States"], "employee_min": 51, "employee_max": 5000}


def _entity(name, domain, desc="An oil and gas company"):
    return Entity(name=name, url=f"https://www.{domain}", description=desc)


def _yes(industry="ind_0", country="c_0", size="within", identity="own_site"):
    def choice(value):
        return Answer("choice", choice=value, probabilities={value: 0.9})

    return {"industry": choice(industry), "hq_country": choice(country),
            "size_band": choice(size), "identity": choice(identity)}


class FakeParallel:
    """Entity Search answers from a list; Decisions from a per-domain table. Records every call."""

    def __init__(self, entities, verdicts=None, *, search_status="ok", decide_status="ok"):
        self.entities = entities
        self.verdicts = verdicts or {}
        self.search_status = search_status
        self.decide_status = decide_status
        self.searches: list[tuple] = []
        self.decided: list[str] = []

    async def entity_search(self, entity_type, objective, *, match_limit=100):
        self.searches.append((entity_type, objective, match_limit))
        if self.search_status != "ok":
            return EntityOutcome(self.search_status, detail="no")
        return EntityOutcome("ok", entities=list(self.entities))

    async def decide(self, state, questions, *, cache_subject=None):
        domain = state["website"].split("//", 1)[-1].removeprefix("www.")
        self.decided.append(domain)
        if self.decide_status != "ok":
            return DecisionOutcome(self.decide_status, detail="no")
        answers = self.verdicts.get(domain, _yes())
        return DecisionOutcome("ok", answers=answers, citations=[
            {"url": f"https://www.{domain}/about", "title": "About"}])


async def _run(tid, n, parallel, *, web=None, icp=ICP):
    async with tenant_session(tid) as ts:
        return await chain.find_icp_companies(
            ts, icp, n, client=PagedClient(), web_search=web or Web(), parallel=parallel)


async def test_parallel_comes_after_linkedin_and_exa_is_only_the_fallback():
    tid = await make_tenant("par1")
    web = Web([CompanyCandidate(name="Webco", domain="webco.io")])
    parallel = FakeParallel([_entity("EOG Resources", "eogresources.com"),
                             _entity("Coterra Energy", "coterra.com")])

    result = await _run(tid, 2, parallel, web=web)

    assert [(c.domain, c.source) for c in result.candidates] == [
        ("eogresources.com", "parallel"), ("coterra.com", "parallel")]
    assert web.calls == 0, "Exa is not asked when Parallel met the request"
    assert parallel.searches == [("companies", pc.objective_for(ICP), 100)], \
        "one search, asking for the 100 results its price includes"


async def test_only_survivors_are_checked_and_only_until_the_request_is_met(monkeypatch):
    tid = await make_tenant("par2")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Held", domain="held-oil.com"))

    async def chevron_homepage(domain):
        return "www.chevron.com", 200, "Chevron Corporation - Human Energy"

    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", chevron_homepage)
    parallel = FakeParallel([
        _entity("Held Oil", "held-oil.com"),
        Entity(name="Some Co", url="https://www.linkedin.com/company/some-co", description=""),
        _entity("American Petroleum Institute", "api.org"),
        _entity("Hess Corporation", "chevron.com"),
        _entity("EOG Resources", "eogresources.com"),
        _entity("Coterra Energy", "coterra.com"),
        _entity("Ovintiv", "ovintiv.com"),
    ])

    result = await _run(tid, 2, parallel)

    assert [c.domain for c in result.candidates] == ["eogresources.com", "coterra.com"]
    assert parallel.decided == ["eogresources.com", "coterra.com"], \
        "the free gates ran first, and Ovintiv was never checked: two were enough"
    assert result.discarded["already_held"] == 1 and result.discarded["no_website"] == 1
    assert result.discarded["association"] == 1 and result.discarded["domain_not_theirs"] == 1


async def test_a_company_that_fails_its_check_gives_way_to_the_next():
    tid = await make_tenant("par3")
    parallel = FakeParallel(
        [_entity("Parker Wellbore", "parkerwellbore.com"), _entity("SPM Oil & Gas", "spmog.com"),
         _entity("Kosmos Energy", "kosmosenergy.com"), _entity("Ovintiv", "ovintiv.com")],
        {"parkerwellbore.com": _yes(identity="parent_site"),
         "spmog.com": _yes(industry="supplier"),
         "kosmosenergy.com": _yes(country="other")})

    result = await _run(tid, 1, parallel)

    assert [c.domain for c in result.candidates] == ["ovintiv.com"]
    assert result.discarded["not_its_own"] == 1
    assert result.discarded["wrong_industry"] == 1 and result.discarded["outside_icp"] == 1


async def test_no_data_on_a_must_match_field_is_no_match():
    tid = await make_tenant("par4")
    parallel = FakeParallel([_entity("Murky Oil", "murkyoil.com"), _entity("EOG", "eog.com")],
                            {"murkyoil.com": _yes(size="unknown")})

    result = await _run(tid, 1, parallel, icp={**ICP, "mandatory": ["size"]})

    assert [c.domain for c in result.candidates] == ["eog.com"]
    assert result.discarded["fails_mandatory"] == 1


async def test_a_supplier_counts_when_the_icp_includes_suppliers():
    tid = await make_tenant("par5")
    parallel = FakeParallel([_entity("SPM Oil & Gas", "spmog.com")],
                            {"spmog.com": _yes(industry="supplier")})

    result = await _run(tid, 1, parallel, icp={**ICP, "include_suppliers": True})

    assert [c.domain for c in result.candidates] == ["spmog.com"]


async def test_every_vetted_company_is_kept_for_the_next_workspace():
    tid = await make_tenant("par6")
    parallel = FakeParallel([_entity("EOG Resources", "eogresources.com"),
                             _entity("Coterra Energy", "coterra.com"),
                             _entity("Ovintiv", "ovintiv.com")])

    await _run(tid, 1, parallel)

    async with get_platform_sessionmaker()() as s:
        stored = {c.domain: c for c in (await s.scalars(select(Company))).all()}
    assert set(stored) == {"eogresources.com", "coterra.com", "ovintiv.com"}, \
        "checked or not, a vetted company a search paid for is kept"
    assert stored["ovintiv.com"].source == "parallel"
    assert stored["ovintiv.com"].description == "An oil and gas company"


async def test_the_cost_of_each_call_is_recorded_and_charged_to_nobody():
    tid = await make_tenant("par7")
    parallel = FakeParallel([_entity("EOG Resources", "eogresources.com"),
                             _entity("Coterra Energy", "coterra.com")])

    await _run(tid, 2, parallel)

    async with get_platform_sessionmaker()() as s:
        rows = (await s.execute(
            select(BillingUsageEvent.capability_id, func.sum(BillingUsageEvent.quantity))
            .where(BillingUsageEvent.tenant_id == tid)
            .group_by(BillingUsageEvent.capability_id))).all()
    assert dict(rows) == {"search.entity": 1, "verify.decision": 2}


async def test_unkeyed_or_out_of_credit_falls_through_to_exa():
    tid = await make_tenant("par8")
    web = Web([CompanyCandidate(name="Webco", domain="webco.io")])
    unkeyed = FakeParallel([], search_status="not_configured")

    result = await _run(tid, 1, unkeyed, web=web)
    assert web.calls == 1 and [c.source for c in result.candidates] == ["web"]
    assert "parallel" not in result.notes, "not configured is silent, as InfoJoy's is"

    broke = FakeParallel([_entity("EOG", "eog.com")], decide_status="out_of_credit")
    result = await _run(await make_tenant("par9"), 1, broke, web=Web(
        [CompanyCandidate(name="Webco", domain="webco.io")]))
    assert result.notes["parallel"] == "out_of_credit"
    assert [c.source for c in result.candidates] == ["web"]


async def test_the_verdict_becomes_the_accounts_evidence():
    """Delivered, the account's industry and country cite the page that showed them, and the
    employee band satisfies a must-match size with no exact count."""
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile
    from nexus.relevance.gate import icp_gate

    tid = await make_tenant("par10")
    icp = {**ICP, "mandatory": ["industry", "country", "size"]}
    parallel = FakeParallel([_entity("EOG Resources", "eogresources.com")])
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, icp, 1, client=PagedClient(),
                                                web_search=Web(), parallel=parallel)
        from nexus.models.relevance import RelevanceProfile

        profile = await get_profile(ts) or RelevanceProfile(tenant_id=tid, icp=icp)
        profile.icp = icp
        ids, _ = await deliver(ts, profile, result.candidates, limit=1, source="discovery",
                               min_fit=70, discarded=Counter())
        account = await ts.get(Account, ids[0])

    assert account.industry == "Oil & Gas" and account.country == "United States"
    assert account.employee_count is None
    assert account.field_sources["industry"]["source"] == "parallel"
    assert account.field_sources["industry"]["ref"] == "https://www.eogresources.com/about"
    assert icp_gate(icp, account).passed, "the band answers size; no number was invented"
