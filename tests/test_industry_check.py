"""Which of the ICP's industries a new company really operates in (owner decision 2026-10-29).

SDR feedback: companies marked with the wrong industry, and companies that work in several. Every
company delivered, whatever found it, gets one check, LinkedIn first: what the shared store's
LinkedIn read says about it, then one Parallel Decisions request carrying that evidence and asking,
per ICP industry, whether the company operates in it, only sells to it, or neither. Parallel's own
finds are skipped (checked when found); with no Parallel key nothing runs, not even a LinkedIn read.
"""
from __future__ import annotations

from collections import Counter

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.integrations.parallel import Answer, DecisionOutcome
from nexus.models import Account
from nexus.models.relevance import RelevanceProfile
from nexus.prospecting import industry_check as ic
from nexus.prospecting import vetting
from nexus.prospecting.companies import Candidate
from nexus.relevance.gate import icp_gate
from tests.conftest import make_tenant, tenant_session

ICP = {"industries": ["Oil & Gas", "Utilities"], "countries": ["United States"]}


def _choice(value, p=0.9):
    return Answer("choice", choice=value, probabilities={value: p})


def _answers(*picks, primary="ind_0", p=0.9):
    out = {f"in_{i}": _choice(v, p) for i, v in enumerate(picks)}
    out["primary"] = _choice(primary, p)
    return out


class FakeDecisions:
    def __init__(self, verdicts, *, configured=True):
        self.verdicts, self._configured, self.states = verdicts, configured, []

    async def configured(self):
        return self._configured

    async def decide(self, state, questions, *, cache_subject=None):
        self.states.append(state)
        domain = state["website"].split("//", 1)[-1]
        return DecisionOutcome("ok", answers=self.verdicts[domain],
                               citations=[{"url": f"https://{domain}/about"}])


def _pair(domain, source="linkedin", industry=None, name=None):
    cand = Candidate(domain=domain, name=name or domain.split(".")[0].title(), source=source,
                     industry=industry)
    return cand, Account(tenant_id="t", name=cand.name, domain=domain, industry=industry)


def test_a_company_in_several_industries_keeps_them_all_main_one_first():
    verdict = ic.judge(_answers("yes", "yes", primary="ind_1"), ICP)
    assert verdict.operates_in == ("Utilities", "Oil & Gas")
    assert ic.judge(_answers("no", "no"), ICP).none is True
    assert ic.judge(_answers("yes", "no", p=0.6), ICP).operates_in == (), "under 0.7 is unknown"


async def test_each_company_is_checked_and_the_verdict_is_its_evidence():
    fake = FakeDecisions({
        "operator.com": _answers("yes", "no"),
        "supplier.com": _answers("supplier", "no"),
        "media.com": _answers("no", "no"),
        "murky.com": _answers("unknown", "unknown"),
    })
    built = [_pair("operator.com", industry="Software Development"),
             _pair("supplier.com", source="web"), _pair("media.com", source="shared"),
             _pair("murky.com", industry="Oil & Gas"), _pair("found.com", source="parallel")]

    result = await ic.check_industries(built, ICP, client=fake)

    accounts = {c.domain: a for c, a in built}
    assert result.wrong == {"supplier.com", "media.com"}
    assert result.decisions == 4
    assert [s["website"] for s in fake.states] == [
        "https://operator.com", "https://supplier.com", "https://media.com", "https://murky.com"], \
        "Parallel's own find was checked when it was found"
    op = accounts["operator.com"]
    assert op.industry == "Oil & Gas" and op.custom_fields["industries"] == ["Oil & Gas"]
    assert op.custom_fields["industry_self_label"] == "Software Development"
    assert op.field_sources["industry"]["source"] == "parallel"
    assert op.field_sources["industry"]["ref"] == "https://operator.com/about"
    assert accounts["supplier.com"].custom_fields["supplier_to"] == ["Oil & Gas"]
    assert accounts["murky.com"].industry == "Oil & Gas", "an unclear answer leaves the label"


async def test_a_supplier_is_kept_when_the_icp_includes_suppliers():
    fake = FakeDecisions({"supplier.com": _answers("supplier", "no")})
    built = [_pair("supplier.com", source="web")]
    icp = {**ICP, "include_suppliers": True}

    result = await ic.check_industries(built, icp, client=fake)

    assert result.wrong == set()
    assert icp_gate({**icp, "mandatory": ["industry"]}, built[0][1]).passed is False, \
        "no industry at all yet"
    built[0][1].industry = "Industrial Machinery"
    assert icp_gate({**icp, "mandatory": ["industry"]}, built[0][1]).passed, \
        "the gate reads what it supplies to"


async def test_linkedin_is_read_first_and_handed_to_the_check():
    from nexus.companies.resolution import resolve_company
    from nexus.prospecting.industries import all_industries, match_term

    code = match_term("Oil and Gas")[0]
    async with get_platform_sessionmaker()() as s:
        row = await resolve_company(s, domain="operator.com", name="Operator", source="linkedin")
        row.linkedin_fetched_at = utcnow()
        row.linkedin_industry_id = code
        row.description = "We drill and operate wells in the Permian Basin."
        await s.commit()
    fake = FakeDecisions({"operator.com": _answers("yes", "no")})

    await ic.check_industries([_pair("operator.com")], ICP, client=fake)

    state, = fake.states
    assert state["linkedin_industry"] == all_industries()[code].label
    assert "Permian" in state["linkedin_description"]


async def test_without_a_parallel_key_nothing_is_read_or_bought(monkeypatch):
    from nexus.prospecting import linkedin as li

    async def must_not_run(*a, **k):
        raise AssertionError("a LinkedIn read with no check to feed")

    monkeypatch.setattr(li, "company_details", must_not_run)
    fake = FakeDecisions({}, configured=False)
    built = [_pair("operator.com")]
    built[0][0].linkedin_url = "https://www.linkedin.com/company/operator"

    result = await ic.check_industries(built, ICP, client=fake)

    assert (result.note, result.wrong, fake.states) == ("not_configured", set(), [])


def test_the_gate_matches_on_any_industry_a_company_operates_in():
    account = Account(tenant_id="t", name="Grid Co", domain="grid.com", industry="Oil & Gas",
                      custom_fields={"industries": ["Oil & Gas", "Utilities"]})
    assert icp_gate({"industries": ["Utilities"], "mandatory": ["industry"]}, account).passed


async def test_delivery_refuses_the_wrong_industry_and_a_site_that_moved(monkeypatch):
    from nexus.prospecting.deliver import deliver

    async def landing(domain):
        return ("www.acquirer.com", 200, "Acquirer") if domain == "sold.com" else (
            f"www.{domain}", 200, "")

    vetting.forget()
    monkeypatch.setattr(vetting, "_fetch_homepage", landing)
    fake = FakeDecisions({"operator.com": _answers("yes", "no"),
                          "media.com": _answers("no", "no")})
    tid = await make_tenant("ic1")
    cands = [Candidate(domain=d, name=n, source="linkedin", industry="Oil & Gas",
                       country="United States")
             for d, n in (("operator.com", "Operator"), ("media.com", "Media"))]
    cands.append(Candidate(domain="sold.com", name="Sold Oil", source="web"))
    async with tenant_session(tid) as ts:
        discarded = Counter()
        ids, _ = await deliver(ts, RelevanceProfile(tenant_id=tid, icp=ICP), cands, limit=5,
                               source="discovery", min_fit=0, discarded=discarded,
                               industry_client=fake)
        names = [(await ts.get(Account, i)).domain for i in ids]

    assert names == ["operator.com"]
    assert discarded["wrong_industry"] == 1 and discarded["lands_elsewhere"] == 1
    assert [s["website"] for s in fake.states] == ["https://operator.com", "https://media.com"], \
        "a site that moved is refused before anything is spent on it"
