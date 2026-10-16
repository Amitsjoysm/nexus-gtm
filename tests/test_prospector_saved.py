"""The Saved tab (decisions D13, D14): the workspace's own records, filtered.

It reads Accounts and Contacts, never the provider: it is free, fast and shows real values, because
it is the tenant's own data. The NEXUS-only filters (email status, lists, signals, funding, job
postings) work here and only here. A filter that workspace records do not store (SIC codes) is
refused with a sentence rather than silently ignored.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug: str) -> tuple[dict, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _book(tid: str) -> dict:
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.signal import SignalEvent
    from nexus.models.workflow import ListItem, ProspectList

    async with tenant_session(tid) as ts:
        acme = Account(name="Acme Example Inc", domain="acme-example.com", industry="Software",
                       employee_count=120, country="United States", region="CA",
                       annual_revenue=25_000_000, tech_stack=["React", "Salesforce"],
                       custom_fields={"city": "San Francisco", "sub_industry": "Developer Tools",
                                      "linkedin_url": "https://linkedin.com/company/acme-example"})
        globex = Account(name="Globex Example", domain="globex-example.io",
                         industry="Financial Services", employee_count=400,
                         country="United States", region="NY", annual_revenue=300_000_000,
                         tech_stack=["HubSpot"], custom_fields={"city": "New York"})
        initech = Account(name="Initech Example", domain="initech-example.co.uk",
                          industry="Software", employee_count=30, country="United Kingdom",
                          custom_fields={"city": "London"})
        for a in (acme, globex, initech):
            ts.add(a)
        await ts.flush()
        people = {
            "john": Contact(account_id=acme.id, full_name="John Smith", title="VP Sales",
                            seniority="VP", email="john@acme-example.com", email_status="valid",
                            phone="+1 415 555 7890",
                            custom_fields={"department": "Sales",
                                           "location": {"country": "United States",
                                                        "state": "CA", "city": "Oakland"}}),
            "priya": Contact(account_id=globex.id, full_name="Priya Raman",
                             title="Head of RevOps", seniority="Director",
                             email="priya@globex-example.io", email_status=None),
            "wei": Contact(account_id=initech.id, full_name="Wei Lindqvist",
                           title="Chief Financial Officer", seniority="C-Level", email=None,
                           phone="+44 20 7946 0123", enrichment_source="sourcing:prospector",
                           custom_fields={"location": {"country": "United Kingdom",
                                                       "city": "London"}}),
        }
        for c in people.values():
            ts.add(c)
        await ts.flush()
        champions = ProspectList(tenant_id=tid, name="Champions", kind="contact", filter={})
        tier1 = ProspectList(tenant_id=tid, name="Tier 1", kind="account", filter={})
        ts.add(champions)
        ts.add(tier1)
        await ts.flush()
        ts.add(ListItem(tenant_id=tid, list_id=champions.id, account_id=acme.id,
                        contact_id=people["john"].id))
        ts.add(ListItem(tenant_id=tid, list_id=tier1.id, account_id=globex.id))
        now = utcnow()
        ts.add(SignalEvent(account_id=acme.id, kind="funding", source="t", title="Raised",
                           dedupe_key="f1", occurred_at=now - timedelta(days=10)))
        ts.add(SignalEvent(account_id=globex.id, kind="hiring", source="t", title="Hiring",
                           dedupe_key="h1", occurred_at=now - timedelta(days=120)))
        await ts.flush()
        return {"acme": acme.id, "globex": globex.id, "initech": initech.id,
                "john": people["john"].id, "priya": people["priya"].id, "wei": people["wei"].id,
                "champions": champions.id, "tier1": tier1.id}


async def _saved(client, h, view="contacts", **body):
    r = await client.post("/api/prospector/saved", headers=h, json={"view": view, **body})
    assert r.status_code == 200, r.text
    return r.json()


def _names(body):
    return sorted(row["name"] for row in body["rows"])


async def test_contacts_show_real_values_newest_first(client):
    h, tid = await _workspace(client, "sv1")
    book = await _book(tid)
    body = await _saved(client, h)
    assert body["total"] == 3 and len(body["rows"]) == 3
    john = next(r for r in body["rows"] if r["id"] == book["john"])
    assert john["email"] == "john@acme-example.com" and john["phone"] == "+1 415 555 7890"
    assert john["company_name"] == "Acme Example Inc" and john["domain"] == "acme-example.com"


@pytest.mark.parametrize("filters, workspace, expected", [
    ({"titles": ["vp"]}, {}, ["John Smith"]),
    ({"seniorities": ["Director"]}, {}, ["Priya Raman"]),
    ({"departments": ["sales"]}, {}, ["John Smith"]),
    ({"company_names": ["globex"]}, {}, ["Priya Raman"]),
    ({"company_domains": ["https://www.acme-example.com/"]}, {}, ["John Smith"]),
    ({"company_cities": ["new york"]}, {}, ["Priya Raman"]),
    ({"company_countries": ["United Kingdom"]}, {}, ["Wei Lindqvist"]),
    ({"person_cities": ["oakland"]}, {}, ["John Smith"]),
    ({"employee_sizes": ["51-200"]}, {}, ["John Smith"]),
    ({"employee_sizes": ["11-50", "201-500"]}, {}, ["Priya Raman", "Wei Lindqvist"]),
    ({"revenue_ranges": ["$100M-$500M"]}, {}, ["Priya Raman"]),
    ({"industries": ["software"]}, {}, ["John Smith", "Wei Lindqvist"]),
    ({"industries": ["software"], "exclude_industries": []}, {}, ["John Smith", "Wei Lindqvist"]),
    ({"exclude_industries": ["software"]}, {}, ["Priya Raman"]),
    ({"technologies": ["react"]}, {}, ["John Smith"]),
    ({"keywords": ["developer"]}, {}, ["John Smith"]),
    ({"has_direct_phone": True}, {}, ["John Smith", "Wei Lindqvist"]),
    ({}, {"email_statuses": ["valid"]}, ["John Smith"]),
    ({}, {"email_statuses": ["none"]}, ["Priya Raman", "Wei Lindqvist"]),
    ({}, {"funding": True}, ["John Smith"]),
    ({}, {"job_postings": True, "signal_days": 180}, ["Priya Raman"]),
    ({}, {"job_postings": True, "signal_days": 90}, []),
    ({}, {"signal_kinds": ["funding", "hiring"], "signal_days": 180},
     ["John Smith", "Priya Raman"]),
])
async def test_each_filter_narrows_the_contacts(client, filters, workspace, expected):
    h, tid = await _workspace(client, f"svf{abs(hash(str((filters, workspace)))) % 100_000}")
    await _book(tid)
    body = await _saved(client, h, filters=filters, workspace=workspace)
    assert _names(body) == expected
    assert body["total"] == len(expected)


async def test_lists_match_named_people_and_whole_accounts(client):
    h, tid = await _workspace(client, "sv2")
    book = await _book(tid)
    body = await _saved(client, h, workspace={"list_ids": [book["champions"], book["tier1"]]})
    assert _names(body) == ["John Smith", "Priya Raman"]
    companies = await _saved(client, h, view="companies",
                             workspace={"list_ids": [book["tier1"]]})
    assert _names(companies) == ["Globex Example"]


async def test_the_companies_view_lists_accounts(client):
    h, tid = await _workspace(client, "sv3")
    await _book(tid)
    body = await _saved(client, h, view="companies", filters={"has_linkedin": True})
    assert _names(body) == ["Acme Example Inc"]
    row = body["rows"][0]
    assert row["kind"] == "account" and row["city"] == "San Francisco"


async def test_pagination_is_exact_and_stable(client):
    from nexus.models.account import Account, Contact

    h, tid = await _workspace(client, "sv4")
    async with tenant_session(tid) as ts:
        a = Account(name="Bulk", domain="bulk-example.com")
        ts.add(a)
        await ts.flush()
        for i in range(30):
            ts.add(Contact(account_id=a.id, full_name=f"Person {i:02d}"))
        await ts.flush()
    first = await _saved(client, h, page=1, page_size=25)
    second = await _saved(client, h, page=2, page_size=25)
    assert first["total"] == second["total"] == 30
    assert len(first["rows"]) == 25 and len(second["rows"]) == 5
    assert not ({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})


async def test_another_workspaces_records_never_appear(client):
    h1, t1 = await _workspace(client, "sv5a")
    await _book(t1)
    h2, _ = await _workspace(client, "sv5b")
    assert (await _saved(client, h2))["total"] == 0


async def test_a_filter_workspace_records_do_not_store_is_refused(client):
    h, tid = await _workspace(client, "sv6")
    r = await client.post("/api/prospector/saved", headers=h,
                          json={"view": "contacts", "filters": {"sic_codes": ["7372"]}})
    assert r.status_code == 422 and "not stored" in r.json()["detail"]


async def test_a_filter_for_the_other_view_is_refused(client):
    h, _ = await _workspace(client, "sv7")
    r = await client.post("/api/prospector/saved", headers=h,
                          json={"view": "companies", "filters": {"titles": ["CFO"]}})
    assert r.status_code == 422


def test_ranges_parse_the_shapes_providers_use():
    from nexus.prospector.ranges import parse_range

    assert parse_range("51-200") == (51, 200)
    assert parse_range("10,001+") == (10001, None)
    assert parse_range("$1M - $10M") == (1_000_000, 10_000_000)
    assert parse_range("$500K-$1M") == (500_000, 1_000_000)
    assert parse_range("Under $1M") == (0, 1_000_000)
    assert parse_range("1B+") == (1_000_000_000, None)
    assert parse_range("lots") is None
