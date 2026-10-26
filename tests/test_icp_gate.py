"""Mandatory ICP fields: only exact matches, whatever the score (SDR feedback, 2026-10-26).

Reported: accounts scored 60-70% fit while failing Industry and Tech Stack, the campaign's main
criteria. The fit score is a weighted mean with no hard criteria, so a miss on the field that
matters most is outvoted by size and geography: industry 0, size 1, geo 1, tech unknown 0.5,
revenue 1 is 65.

Decided with the owner: Industry, Size, Country/Region, Tech stack and Job titles can each be
marked mandatory; a mandatory field must match exactly, and exact means the SAME MEANING (a child
industry code counts, "VP Sales" is "Vice President of Sales", "HubSpot CRM" is HubSpot); no data
on a mandatory field is not a match; country is the headquarters.
"""
from __future__ import annotations

import pytest

from nexus.models.account import Account
from nexus.relevance.gate import icp_gate, same_meaning_title, tech_terms

ICP = {"industries": ["Oil & Gas"], "linkedin_industry_ids": [57], "countries": ["United States"],
       "employee_min": 50, "employee_max": 5000,
       "required_tech": ["Salesforce", "HubSpot"]}


def _acct(**kw):
    base = dict(tenant_id="t", name="Acme", domain="acme.io", industry="Oil and Gas",
                country="United States", employee_count=400, tech_stack=["HubSpot CRM"])
    base.update(kw)
    return Account(**base)


def test_nothing_mandatory_gates_nothing():
    gate = icp_gate(ICP, _acct(industry="Software Development", tech_stack=[]))
    assert gate.passed and gate.failed == [] and gate.unknown == []


def test_a_mandatory_miss_fails_however_well_the_rest_scores():
    from nexus.relevance.engine import get_relevance_engine
    from types import SimpleNamespace

    icp = {**ICP, "mandatory": ["industry", "tech"], "revenue_min": 1, "revenue_max": 10**9}
    account = _acct(industry="Industrial Machinery Manufacturing", tech_stack=[],
                    annual_revenue=50_000_000)
    assert get_relevance_engine().score_icp_fit(SimpleNamespace(icp=icp), account).score >= 60

    gate = icp_gate(icp, account)

    assert not gate.passed
    assert gate.failed == ["industry"] and gate.unknown == ["tech"]
    assert gate.reason == "Fails Industry; no data for Tech stack"


@pytest.mark.parametrize("industry", ["Oil and Gas", "Oil Extraction", "Natural Gas Extraction",
                                      "oil & gas"])
def test_same_meaning_counts_a_child_code_and_another_spelling(industry):
    assert icp_gate({**ICP, "mandatory": ["industry"]}, _acct(industry=industry)).passed


def test_the_website_verdict_decides_over_the_self_label():
    """The account's industry IS the verdict once industry evidence is on; a supplier's own
    "Oil and Gas" is kept aside and counts only when the ICP includes suppliers."""
    icp = {**ICP, "mandatory": ["industry"]}
    maker = _acct(industry="Agriculture, Construction, Mining Machinery Manufacturing",
                  custom_fields={"industry_self_label": "Oil and Gas"})
    assert not icp_gate(icp, maker).passed
    assert icp_gate({**icp, "include_suppliers": True}, maker).passed


@pytest.mark.parametrize("count, ok", [(50, True), (5000, True), (49, False), (5001, False)])
def test_size_must_sit_inside_the_band(count, ok):
    assert icp_gate({**ICP, "mandatory": ["size"]}, _acct(employee_count=count)).passed is ok


def test_no_headcount_is_no_match_when_size_is_mandatory():
    gate = icp_gate({**ICP, "mandatory": ["size"]}, _acct(employee_count=None))
    assert not gate.passed and gate.unknown == ["size"]


@pytest.mark.parametrize("country, ok", [("United States", True), ("USA", True),
                                         ("U.S.", True), ("Canada", False)])
def test_country_is_the_headquarters_with_its_aliases(country, ok):
    assert icp_gate({**ICP, "mandatory": ["country"]}, _acct(country=country)).passed is ok


def test_region_reads_the_column_then_enrichment():
    icp = {**ICP, "regions": ["Texas"], "mandatory": ["region"]}
    assert icp_gate(icp, _acct(region="Texas")).passed
    assert icp_gate(icp, _acct(custom_fields={"region": "texas"})).passed
    assert not icp_gate(icp, _acct(region="Oklahoma")).passed
    assert icp_gate(icp, _acct()).unknown == ["region"]


@pytest.mark.parametrize("stack, ok", [
    (["HubSpot CRM"], True), (["Salesforce Sales Cloud"], True), (["SFDC"], True),
    (["salesforce.com"], True), (["Pipedrive"], False),
])
def test_tech_matches_by_product_alias_any_of(stack, ok):
    assert icp_gate({**ICP, "mandatory": ["tech"]}, _acct(tech_stack=stack)).passed is ok


def test_all_of_is_an_ICP_choice():
    icp = {**ICP, "mandatory": ["tech"], "tech_match": "all"}
    assert not icp_gate(icp, _acct(tech_stack=["HubSpot"])).passed
    assert icp_gate(icp, _acct(tech_stack=["HubSpot", "Salesforce"])).passed


def test_a_grouped_tech_entry_means_any_product_it_names():
    """The Relevance form split "CRM (Salesforce, HubSpot, Zoho)" at its commas into fragments that
    could never match; a group is read as its products."""
    assert tech_terms("CRM (Salesforce, HubSpot, Zoho)") == ["salesforce", "hubspot", "zoho"]
    assert tech_terms("Marketing Automation (Marketo") == ["marketo"]
    assert tech_terms("Zoho)") == ["zoho"]
    icp = {**ICP, "required_tech": ["CRM (Salesforce, HubSpot, Zoho)"], "mandatory": ["tech"]}
    assert icp_gate(icp, _acct(tech_stack=["Zoho CRM"])).passed


@pytest.mark.parametrize("title, wanted, ok", [
    ("Vice President of Sales, EMEA", "VP Sales", True),
    ("VP, Sales", "Vice President of Sales", True),
    ("Manager, Operations", "Operations Manager", True),
    ("Senior Operations Manager", "Operations Manager", True),
    ("Operations Director", "Director of Operations", True),
    ("Lead Operator", "Operations Manager", False),
    ("Manager", "Operations Manager", False),
    ("Sales Manager", "VP Sales", False),
    ("Land Manager", "Operations Manager", False),
    ("Chief Revenue Officer", "CRO", True),
])
def test_titles_match_on_meaning_not_on_loose_overlap(title, wanted, ok):
    assert same_meaning_title(title, [wanted]) is ok


def test_titles_mandatory_tightens_who_a_contact_search_keeps():
    from nexus.lookalike.contacts import icp_title_targets, title_fits

    loose = icp_title_targets({"buyer_titles": ["Operations Manager"]})
    strict = icp_title_targets({"buyer_titles": ["Operations Manager"], "mandatory": ["titles"]})
    assert title_fits("Land Manager", loose) is True        # today's keyword overlap
    assert title_fits("Land Manager", strict) is False
    assert title_fits("Senior Operations Manager", strict) is True


# ---- delivery: every source, LinkedIn included -------------------------------------------------

async def _deliver(monkeypatch, icp, cands, *, enrich=None):
    from collections import Counter

    from nexus.core.config import get_settings
    from nexus.discovery import auto
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile
    from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

    enriched: list[str] = []

    async def fake_enrich(ts, accounts, *, concurrency):
        for account in accounts:
            enriched.append(account.domain)
            if enrich:
                enrich(account)

    monkeypatch.setattr(auto, "_enrich_candidates", fake_enrich)
    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", True)
    tid = await make_tenant(f"gate-{len(cands)}-{'-'.join(sorted(icp.get('mandatory', [])))}")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=icp)
    discarded = Counter()
    async with tenant_session(tid) as ts:
        ids, _ = await deliver(ts, await get_profile(ts), cands, limit=10,
                               source="auto_discovery", min_fit=0, discarded=discarded)
        from nexus.models.account import Account

        domains = sorted([(await ts.get(Account, i)).domain for i in ids])
    return domains, discarded, enriched


def _li(domain, industry="Oil and Gas", **kw):
    from nexus.prospecting.companies import Candidate

    return Candidate(domain=domain, name=domain, source="linkedin", industry=industry,
                     country=kw.pop("country", "United States"),
                     employee_count=kw.pop("employee_count", 300),
                     linkedin_url=f"https://www.linkedin.com/company/{domain}/", **kw)


async def test_a_linkedin_company_failing_a_mandatory_field_is_not_delivered(monkeypatch):
    """LinkedIn candidates were "ICP matches by construction" and skipped every gate."""
    icp = {**ICP, "mandatory": ["industry", "country"]}
    domains, discarded, _ = await _deliver(monkeypatch, icp, [
        _li("producer.io"), _li("software.io", industry="Software Development"),
        _li("abroad.io", country="Canada")])

    assert domains == ["producer.io"]
    assert discarded["fails_mandatory"] == 2


async def test_a_mandatory_field_with_no_data_is_looked_up_before_it_is_judged(monkeypatch):
    icp = {**ICP, "mandatory": ["tech"]}

    def enrich(account):
        if account.domain == "uses-hubspot.io":
            account.tech_stack = ["HubSpot"]

    domains, discarded, enriched = await _deliver(
        monkeypatch, icp, [_li("uses-hubspot.io"), _li("unknown-stack.io")], enrich=enrich)

    assert sorted(enriched) == ["unknown-stack.io", "uses-hubspot.io"], "LinkedIn rows enriched"
    assert domains == ["uses-hubspot.io"]
    assert discarded["fails_mandatory"] == 1


async def test_nothing_mandatory_delivers_as_before(monkeypatch):
    domains, discarded, enriched = await _deliver(
        monkeypatch, ICP, [_li("a.io"), _li("b.io", industry="Software Development")])

    assert domains == ["a.io", "b.io"] and not discarded["fails_mandatory"]
    assert enriched == [], "LinkedIn rows are not enriched unless a mandatory field needs it"


# ---- the API: saving the options, and every account saying how it fares -------------------------

async def _owner(client, slug):
    from tests.conftest import auth, signup

    token = await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.title())
    return auth(token)


async def test_saving_keeps_only_known_fields_and_real_switches(client):
    h = await _owner(client, "gate-api1")
    r = await client.put("/api/relevance/profile", headers=h, json={"icp": {
        "industries": ["Oil & Gas"], "required_tech": ["HubSpot"],
        "mandatory": ["industry", "tech", "revenue", "industry"],
        "include_suppliers": "false", "tech_match": "most"}})
    assert r.status_code == 200, r.text
    icp = r.json()["icp"]

    assert icp["mandatory"] == ["industry", "tech"]
    # A JSON string "false" is truthy in Python: anything not a real boolean is dropped.
    assert "include_suppliers" not in icp and "tech_match" not in icp

    r = await client.put("/api/relevance/profile", headers=h, json={"icp": {
        **icp, "include_suppliers": True, "tech_match": "all"}})
    assert r.json()["icp"]["include_suppliers"] is True
    assert r.json()["icp"]["tech_match"] == "all"


async def test_an_account_says_which_mandatory_field_it_fails(client):
    h = await _owner(client, "gate-api2")
    await client.put("/api/relevance/profile", headers=h, json={"icp": {
        "industries": ["Oil & Gas"], "required_tech": ["HubSpot"],
        "mandatory": ["industry", "tech"]}})
    made = await client.post("/api/accounts", headers=h, json={
        "name": "Valve Co", "domain": "valveco.io", "industry": "Machinery Manufacturing"})
    assert made.status_code in (200, 201), made.text
    assert made.json()["icp_gate"] == {
        "passed": False, "failed": ["industry"], "unknown": ["tech"],
        "reason": "Fails Industry; no data for Tech stack"}

    listed = (await client.get("/api/accounts", headers=h)).json()
    rows = listed if isinstance(listed, list) else listed["items"]
    assert rows[0]["icp_gate"]["reason"] == "Fails Industry; no data for Tech stack"
    one = (await client.get(f"/api/accounts/{made.json()['id']}", headers=h)).json()
    assert one["icp_gate"]["passed"] is False


async def test_no_mandatory_field_means_no_gate_on_the_account(client):
    h = await _owner(client, "gate-api3")
    made = await client.post("/api/accounts", headers=h, json={"name": "Any", "domain": "any.io"})
    assert made.json()["icp_gate"] is None


# ---- the screens (no frontend test runner: these read the source) --------------------------------

def _src(path):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "frontend" / "src" / path).read_text("utf-8")


def test_the_browser_names_the_fields_as_the_server_does():
    import re

    from nexus.relevance.gate import FIELDS, LABELS

    block = _src("lib/types.ts").split("MANDATORY_LABELS", 1)[1].split("};", 1)[0]
    labels = dict(re.findall(r'(\w+): "([^"]+)"', block))
    assert labels == LABELS and tuple(labels) == FIELDS


def test_a_miss_is_shown_as_the_field_never_as_a_score():
    accounts = _src("pages/AccountsPage.tsx")
    assert "<FitBadge score={a.fit_score} gate={a.icp_gate} />" in accounts
    assert "fitSortValue(a.fit_score, a.icp_gate)" in accounts
    detail = _src("pages/AccountDetailPage.tsx")
    assert "acc.icp_gate.reason" in detail


def test_the_form_keeps_a_bracketed_group_as_one_entry():
    page = _src("pages/RelevancePage.tsx")
    split = page.split("const splitCsv", 1)[1].split("};", 1)[0]
    assert 'ch === "("' in split and "depth === 0" in split


# ---- campaigns: a must-match field holds when people are added ----------------------------------

async def _enroll_with(slug, icp):
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.relevance.engine import get_profile
    from tests.conftest import tenant_session
    from tests.test_engagement_sequences import STEPS, _world

    tid, user_id, mailbox_id, ids = await _world(slug, contacts=2)
    async with tenant_session(tid) as ts:
        (await get_profile(ts)).icp = icp
        await ts.flush()
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        return ids, await enroll(ts, campaign, ids)


async def test_people_at_a_company_outside_a_must_match_field_are_not_added():
    # The world's account is in Germany with no industry on record: no data is no match.
    ids, result = await _enroll_with("gate-enr1", {
        "industries": ["Software Development"], "countries": ["Germany"],
        "mandatory": ["industry", "country"]})

    assert result.added == []
    assert {s["reason"] for s in result.skipped} == {"outside_icp"}


async def test_a_mandatory_title_holds_for_the_person():
    ids, result = await _enroll_with("gate-enr2", {
        "buyer_titles": ["Chief Revenue Officer"], "mandatory": ["titles"]})
    assert result.added == [] and {s["reason"] for s in result.skipped} == {"title_outside_icp"}

    ids, result = await _enroll_with("gate-enr3", {
        "buyer_titles": ["VP Engineering"], "countries": ["Germany"],
        "mandatory": ["titles", "country"]})
    assert result.added == ids


async def test_nothing_mandatory_adds_as_before():
    ids, result = await _enroll_with("gate-enr4", {"industries": ["Software Development"]})
    assert result.added == ids
