"""New ICP companies from Parallel: Entity Search finds, one Decisions request per company checks
(owner decisions 2026-10-29, `docs/superpowers/specs/2026-10-29-parallel-provider-design.md`).

The chain is the shared store, then LinkedIn, then Parallel, then Exa as the fallback. Every call
counts: one search asks for the 100 results its price includes, only candidates that survive the
free gates are checked, only until the request is met, and every company whose check says it
operates under its own name is kept in the shared store for the next workspace.

The entities here have the shape the first real answers had (2026-10-29): the ``url`` is the
company's LinkedIn page and the website, stage and HQ are fields of the description. The values
are synthetic.
"""
from __future__ import annotations

from collections import Counter

import pytest
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


def _entity(name, domain, desc="An oil and gas company", *, stage="Public",
            country="United States", headcount=None):
    slug = name.lower().replace(" ", "-")
    count = f"Employee Count Official: {headcount} | " if headcount is not None else ""
    return Entity(name=name, url=f"https://linkedin.com/company/{slug}", description=(
        f"Name: {name} | Website Url: https://www.{domain} | Primary Sector: Energy | "
        f"Primary Subsector: Oil & Gas Upstream | Location Country: {country} | "
        f"Location City: Houston | Location State: Texas | Stage: {stage} | "
        f"{count}Short Description: {desc}"))


#: Where a homepage lands, by domain; anything not listed lands on itself, None does not answer.
LANDINGS: dict[str, tuple[str, int, str] | None] = {}


@pytest.fixture(autouse=True)
def _homepages(monkeypatch):
    """Homepages are read before each check; offline, they answer from LANDINGS."""
    async def fetch(domain):
        landing = LANDINGS.get(domain, (f"www.{domain}", 200, ""))
        if landing is None:
            raise OSError("no answer")
        return landing

    vetting.forget()
    LANDINGS.clear()
    monkeypatch.setattr(vetting, "_fetch_homepage", fetch)
    yield
    LANDINGS.clear()


def _yes(industry="ind_0", country="c_0", size="within", identity="own_site", p=0.9):
    def choice(value):
        return Answer("choice", choice=value, probabilities={value: p})

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
        self.states: list[dict] = []

    async def entity_search(self, entity_type, objective, *, match_limit=100):
        self.searches.append((entity_type, objective, match_limit))
        if self.search_status != "ok":
            return EntityOutcome(self.search_status, detail="no")
        return EntityOutcome("ok", entities=list(self.entities))

    async def decide(self, state, questions, *, cache_subject=None):
        domain = state["website"].split("//", 1)[-1].removeprefix("www.")
        self.decided.append(domain)
        self.states.append(state)
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


async def test_only_survivors_are_checked_and_only_until_the_request_is_met():
    tid = await make_tenant("par2")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Held", domain="held-oil.com"))

    LANDINGS["chevron.com"] = ("www.chevron.com", 200, "Chevron Corporation - Human Energy")
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


async def test_the_record_refuses_a_defunct_company_and_a_foreign_hq_for_free():
    """The website comes from the record, not the LinkedIn link; a company the record calls
    Deadpooled, or headquartered outside the ICP's countries, is never checked."""
    tid = await make_tenant("par2b")
    parallel = FakeParallel([
        _entity("Gone Petroleum", "gonepetroleum.com", stage="Deadpooled"),
        _entity("Maple Oil", "mapleoil.ca", country="Canada"),
        _entity("EOG Resources", "eogresources.com"),
    ])

    result = await _run(tid, 1, parallel)

    assert [c.domain for c in result.candidates] == ["eogresources.com"]
    assert parallel.decided == ["eogresources.com"]
    assert result.discarded["defunct"] == 1 and result.discarded["outside_icp"] == 1
    state, = parallel.states
    assert state == {"company": "EOG Resources", "website": "https://eogresources.com",
                     "headquarters": "Houston, Texas, United States",
                     "description": "An oil and gas company"}, \
        "the check is told what the company is, not the record's often-stale stage or headcount"


async def test_a_homepage_that_lands_on_another_company_is_refused_without_a_check():
    """Measured: cabotog.com lands on coterra.com, and Parallel's check had called Cabot operating.
    A site that does not answer is no evidence either way (Devon's did not, from here)."""
    tid = await make_tenant("par2d")
    LANDINGS["cabotog.com"] = ("www.coterra.com", 200, "Coterra Energy")
    LANDINGS["bhge.com"] = ("www.bakerhughes.com", 200, "Baker Hughes")
    LANDINGS["devonenergy.com"] = None
    parallel = FakeParallel([_entity("Cabot Oil", "cabotog.com"),
                             _entity("Baker Hughes", "bhge.com"),
                             _entity("Devon Energy", "devonenergy.com")])

    result = await _run(tid, 3, parallel)

    assert [c.domain for c in result.candidates] == ["bhge.com", "devonenergy.com"], \
        "a move to a domain naming the company is a rebrand, not a sale"
    assert parallel.decided == ["bhge.com", "devonenergy.com"]
    assert result.discarded["lands_elsewhere"] == 1


async def test_identity_and_industry_need_more_than_even_odds():
    tid = await make_tenant("par2e")
    parallel = FakeParallel(
        [_entity("Midstates Petroleum", "midstatespetroleum.com"),
         _entity("SPM Oil & Gas", "spmog.com"),
         _entity("Bison Oil & Gas", "bisonog.com", stage="Acquired"),
         _entity("Murphy Oil", "murphyoilcorp.com", stage="Acquired"),
         _entity("EOG Resources", "eogresources.com")],
        {"midstatespetroleum.com": _yes(p=0.56), "spmog.com": {**_yes(), "industry": Answer(
            "choice", choice="ind_0", probabilities={"ind_0": 0.51})},
         "bisonog.com": _yes(p=0.9), "murphyoilcorp.com": _yes(p=0.99)})

    result = await _run(tid, 2, parallel)

    assert [c.domain for c in result.candidates] == ["murphyoilcorp.com", "eogresources.com"]
    assert result.discarded["unconfirmed"] == 2, \
        "0.56 is not proof, nor 0.9 when the record says Acquired"
    assert result.discarded["industry_unclear"] == 1


async def test_an_acquired_or_defunct_verdict_says_so():
    tid = await make_tenant("par2c")
    parallel = FakeParallel(
        [_entity("Marathon Oil", "marathonoil.com"), _entity("Cobalt Energy", "cobaltenergy.com"),
         _entity("EOG Resources", "eogresources.com")],
        {"marathonoil.com": _yes(identity="acquired"),
         "cobaltenergy.com": _yes(identity="defunct")})

    result = await _run(tid, 1, parallel)

    assert [c.domain for c in result.candidates] == ["eogresources.com"]
    assert result.discarded["acquired"] == 1 and result.discarded["defunct"] == 1
    assert "operate today" in pc.questions_for(ICP)["identity"]["instructions"]


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


async def test_a_size_the_check_could_not_answer_is_looked_up_not_refused():
    """Measured: the check could not size 38 of 79 companies, Diamondback, EOG and Devon among
    them. Unanswered is passed on as ``unchecked``; delivery looks it up and screens it, and a
    MUST-MATCH size still without data there is no match (D3)."""
    from nexus.models.relevance import RelevanceProfile
    from nexus.prospecting.deliver import deliver

    tid = await make_tenant("par4")
    icp = {**ICP, "mandatory": ["size"]}
    parallel = FakeParallel([_entity("Murky Oil", "murkyoil.com"), _entity("EOG", "eog.com")],
                            {"murkyoil.com": _yes(size="unknown")})
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, icp, 2, client=PagedClient(),
                                                web_search=Web(), parallel=parallel)
        assert [(c.domain, c.evidence["unchecked"]) for c in result.candidates] == [
            ("murkyoil.com", ["size"]), ("eog.com", [])]
        discarded = Counter()
        ids, _ = await deliver(ts, RelevanceProfile(tenant_id=tid, icp=icp), result.candidates,
                               limit=2, source="discovery", min_fit=70, discarded=discarded)
        names = [(await ts.get(Account, i)).domain for i in ids]

    assert names == ["eog.com"]
    assert discarded["fails_mandatory"] == 1


async def test_the_records_headcount_answers_a_size_the_check_could_not():
    """Only where the check was silent: Delek's record read 6 while the check said within, and the
    check wins."""
    tid = await make_tenant("par4c")
    parallel = FakeParallel(
        [_entity("Tiny Oil", "tinyoil.com", headcount=2),
         _entity("Ring Energy", "ringenergy.com", headcount=88),
         _entity("Delek US", "delekus.com", headcount=6)],
        {"tinyoil.com": _yes(size="unknown"), "ringenergy.com": _yes(size="unknown")})

    result = await _run(tid, 2, parallel)

    assert [(c.domain, c.employee_count, c.evidence["unchecked"]) for c in result.candidates] == [
        ("ringenergy.com", 88, []), ("delekus.com", None, [])]
    assert result.discarded["outside_icp"] == 1
    state = parallel.states[0]
    assert "6" not in str(state) and "Employee" not in str(state)


async def test_an_unchecked_size_is_screened_and_a_checked_one_is_trusted():
    """No fit threshold either way: the industry was checked. Only the size the check left open
    is held to the ICP's band, with what delivery knows."""
    from nexus.models.relevance import RelevanceProfile
    from nexus.prospecting.companies import Candidate
    from nexus.prospecting.deliver import deliver

    tid = await make_tenant("par4b")
    giant = Candidate(domain="giant-oil.com", name="Giant Oil", source="parallel",
                      employee_count=60000, industry="Oil & Gas", country="United States",
                      evidence={"unchecked": ["size"]})
    banded = Candidate(domain="banded-oil.com", name="Banded Oil", source="parallel",
                       industry="Oil & Gas", country="United States",
                       evidence={"band": (51, 5000), "unchecked": []})
    async with tenant_session(tid) as ts:
        discarded = Counter()
        ids, _ = await deliver(ts, RelevanceProfile(tenant_id=tid, icp=ICP), [giant, banded],
                               limit=2, source="discovery", min_fit=99, discarded=discarded)
        names = [(await ts.get(Account, i)).domain for i in ids]

    assert names == ["banded-oil.com"]
    assert discarded["outside_icp"] == 1


async def test_a_supplier_counts_when_the_icp_includes_suppliers():
    tid = await make_tenant("par5")
    parallel = FakeParallel([_entity("SPM Oil & Gas", "spmog.com")],
                            {"spmog.com": _yes(industry="supplier")})

    result = await _run(tid, 1, parallel, icp={**ICP, "include_suppliers": True})

    assert [c.domain for c in result.candidates] == ["spmog.com"]


async def test_a_company_checked_as_operating_under_its_own_name_is_kept_for_the_next_workspace():
    """Kept whatever the check says about THIS ICP (another workspace may sell to a supplier);
    never one the check called acquired, nor one nobody checked: other workspaces are offered what
    the store holds."""
    tid = await make_tenant("par6")
    parallel = FakeParallel(
        [_entity("Marathon Oil", "marathonoil.com"), _entity("SPM Oil & Gas", "spmog.com"),
         _entity("EOG Resources", "eogresources.com"), _entity("Ovintiv", "ovintiv.com")],
        {"marathonoil.com": _yes(identity="acquired"), "spmog.com": _yes(industry="supplier")})

    await _run(tid, 1, parallel)

    async with get_platform_sessionmaker()() as s:
        stored = {c.domain: c for c in (await s.scalars(select(Company))).all()}
    assert set(stored) == {"spmog.com", "eogresources.com"}
    assert stored["spmog.com"].source == "parallel"
    assert stored["spmog.com"].description == "An oil and gas company", \
        "what it does, not the whole record"


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
    assert account.employee_count is None, "the record's headcount (6) is not taken"
    assert account.field_sources["industry"]["source"] == "parallel"
    assert account.field_sources["industry"]["ref"] == "https://www.eogresources.com/about"
    assert icp_gate(icp, account).passed, "the band answers size; no number was invented"
