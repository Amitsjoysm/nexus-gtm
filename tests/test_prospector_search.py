"""Searching Prospector: Total and Net new, masked rows, held rows, limits, and the no-PII proof.

Offline: the provider is ``FakeVendor`` behind a real ``InfoJoyClient``, installed through the
search module's test seam. The proof that matters most is structural and end to end: no email,
phone, domain or LinkedIn value the workspace has not paid for appears anywhere in a search
response, on any tab, with or without a count.
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.prospector_fixtures import CONTACTS, SECRETS, FakeVendor, many_contacts


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


@pytest.fixture(autouse=True)
def _clean_switches():
    from nexus.features.switches import invalidate

    invalidate()
    yield
    invalidate()


async def _workspace(client, slug: str) -> tuple[dict, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _search(client, h, **body):
    return await client.post("/api/prospector/search", headers=h,
                             json={"view": "contacts", **body})


async def _hold_john(tid: str) -> str:
    from nexus.models.account import Account, Contact

    async with tenant_session(tid) as ts:
        acme = Account(name="Acme Example Inc", domain="acme-example.com")
        ts.add(acme)
        await ts.flush()
        john = Contact(account_id=acme.id, full_name="John Smith",
                       email="john.smith@acme-example.com", phone="+1 415 555 7890")
        ts.add(john)
        await ts.flush()
        return john.id


# ---- Total -------------------------------------------------------------------------------------

async def test_total_returns_masked_rows_with_business_card_facts(client, vendor):
    h, _ = await _workspace(client, "ps1")
    r = await _search(client, h, filters={"titles": ["VP Sales"]}, page_size=25)

    assert r.status_code == 200, r.text
    body = r.json()
    first = body["rows"][0]
    assert (first["first_name"], first["last_name"], first["title"]) == \
        ("John", "Smith", "VP Sales")
    assert first["email_masked"] == "j•••@a•••.com" and first["has_email"] is True
    assert first["phone_masked"] == "•••-•••-7890" and first["has_direct_phone"] is True
    assert first["domain_masked"] == "a•••.com" and first["saved"] is False
    assert len(first["row_key"]) == 32 and first["api_page"] == 1
    assert body["has_more"] is False and body["total"] is None
    assert vendor.requests[0]["body"]["Filters"] == {"Titles": ["VP Sales"]}


async def test_a_full_page_says_there_is_more(client, vendor):
    vendor.contacts = many_contacts(60)
    h, _ = await _workspace(client, "ps2")
    body = (await _search(client, h, page_size=25, page=2)).json()
    assert len(body["rows"]) == 25 and body["has_more"] is True and body["page"] == 2


async def test_a_count_is_asked_for_only_explicitly(client, vendor, monkeypatch):
    from nexus.prospector import client as prospector_client

    # Where the provider can count; it cannot today (test_prospector_observed_provider.py).
    monkeypatch.setitem(prospector_client.COUNT_SUPPORTED, "contacts", True)
    h, _ = await _workspace(client, "ps3")
    body = (await _search(client, h, include_count=True)).json()
    assert body["total"] == len(CONTACTS)
    assert vendor.requests[0]["body"]["IncludeTotalCount"] is True


async def test_the_companies_view_masks_domains_and_linkedin(client, vendor):
    h, _ = await _workspace(client, "ps4")
    r = await client.post("/api/prospector/search", headers=h,
                          json={"view": "companies", "filters": {"company_countries": ["United States"]}})
    rows = r.json()["rows"]
    assert rows[0]["company_name"] == "Acme Example Inc"
    assert rows[0]["linkedin_masked"] == "linkedin.com/company/a•••"
    assert rows[0]["industry"] == "Software" and rows[0]["employee_size_range"] == "51-200"
    assert vendor.requests[0]["path"].endswith("/companies")


async def test_a_held_row_is_saved_with_the_workspaces_own_values(client, vendor):
    h, tid = await _workspace(client, "ps5")
    john_id = await _hold_john(tid)
    rows = (await _search(client, h)).json()["rows"]
    john = rows[0]
    assert john["saved"] is True
    assert john["record"] == {"kind": "contact", "id": john_id, "account_id": john["record"]["account_id"],
                              "email": "john.smith@acme-example.com", "phone": "+1 415 555 7890",
                              "domain": "acme-example.com"}
    assert all(r["saved"] is False for r in rows[1:])


# ---- the proof ---------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"view": "contacts"},
    {"view": "contacts", "include_count": True},
    {"view": "contacts", "scope": "net_new"},
    {"view": "companies"},
    {"view": "companies", "scope": "net_new", "include_count": False},
])
async def test_no_unmasked_value_appears_in_any_search_response(client, vendor, body):
    h, _ = await _workspace(client, f"pii{abs(hash(str(body))) % 10_000}")
    r = await client.post("/api/prospector/search", headers=h, json=body)
    assert r.status_code == 200, r.text
    text = r.text.lower()
    leaked = [s for s in SECRETS if s.lower() in text]
    assert not leaked, f"unmasked values reached the browser: {leaked}"


# ---- Net new -----------------------------------------------------------------------------------

async def test_net_new_hides_held_rows_and_fills_the_page_from_later_pages(client, vendor, monkeypatch):
    vendor.contacts = many_contacts(300)
    h, tid = await _workspace(client, "ps6")
    from nexus.models.account import Account, Contact

    async with tenant_session(tid) as ts:
        a = Account(name="Bulk", domain="bulk0-example.com")
        ts.add(a)
        await ts.flush()
        for i in range(0, 120):                        # hold the first 120
            ts.add(Contact(account_id=a.id, full_name=f"p{i} Example",
                           email=f"p{i}@bulk-example.com"))
        await ts.flush()

    body = (await _search(client, h, scope="net_new", page_size=25)).json()

    names = [r["first_name"] for r in body["rows"]]
    assert names == [f"p{i}" for i in range(120, 145)]
    assert all(r["saved"] is False for r in body["rows"])
    assert body["scanned"] == 145 and body["next_cursor"] and not body["exhausted"]
    nxt = (await _search(client, h, scope="net_new", page_size=25,
                         cursor=body["next_cursor"])).json()
    assert [r["first_name"] for r in nxt["rows"]][:2] == ["p145", "p146"]


async def test_net_new_stops_at_the_scan_cap_and_says_so(client, vendor, monkeypatch):
    from nexus.models.account import Account, Contact

    monkeypatch.setattr(get_settings(), "prospector_net_new_scan_rows", 100)
    vendor.contacts = many_contacts(300)
    h, tid = await _workspace(client, "ps7")
    async with tenant_session(tid) as ts:
        a = Account(name="Bulk", domain="bulk0-example.com")
        ts.add(a)
        await ts.flush()
        for i in range(0, 250):
            ts.add(Contact(account_id=a.id, full_name=f"p{i} Example",
                           email=f"p{i}@bulk-example.com"))
        await ts.flush()

    body = (await _search(client, h, scope="net_new", page_size=25)).json()
    assert body["rows"] == [] and body["scanned"] == 100 and body["scan_capped"] is True
    assert body["next_cursor"]


async def test_net_new_reports_exhaustion(client, vendor):
    h, _ = await _workspace(client, "ps8")
    body = (await _search(client, h, scope="net_new", page_size=25)).json()
    assert len(body["rows"]) == len(CONTACTS) and body["exhausted"] is True
    assert body["next_cursor"] is None


async def test_a_cursor_from_another_workspace_or_edited_is_refused(client, vendor):
    vendor.contacts = many_contacts(300)
    h1, _ = await _workspace(client, "ps9a")
    h2, _ = await _workspace(client, "ps9b")
    cursor = (await _search(client, h1, scope="net_new", page_size=25)).json()["next_cursor"]

    assert (await _search(client, h2, scope="net_new", cursor=cursor)).status_code == 400
    assert (await _search(client, h1, scope="net_new", cursor=cursor[:-2] + "xx")).status_code == 400
    # A cursor belongs to the filters it was made for.
    changed = await _search(client, h1, scope="net_new", cursor=cursor,
                            filters={"titles": ["CFO"]})
    assert changed.status_code == 400


# ---- refusals and limits -----------------------------------------------------------------------

async def test_not_configured_is_a_clear_400(client):
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    search.set_client(InfoJoyClient(keys=[], transport=FakeVendor().transport()))
    try:
        h, _ = await _workspace(client, "ps10")
        r = await _search(client, h)
    finally:
        search.set_client(None)
    assert r.status_code == 400 and "Provider keys" in r.json()["detail"]


async def test_a_provider_outage_is_a_503_and_nothing_is_counted(client, vendor):
    from nexus.prospector.limits import rows_today

    vendor.status = 503
    h, tid = await _workspace(client, "ps11")
    r = await _search(client, h)
    assert r.status_code == 503
    async with tenant_session(tid) as ts:
        assert await rows_today(ts) == 0


async def test_rows_served_count_against_the_budget_and_vendor_rows_are_recorded(client, vendor):
    from nexus.models.billing import BillingUsageEvent
    from nexus.prospector.limits import rows_today

    h, tid = await _workspace(client, "ps12")
    await _search(client, h, page_size=25)
    await _search(client, h, page_size=25)              # served from the shared cache

    async with tenant_session(tid) as ts:
        assert await rows_today(ts) == 2 * len(CONTACTS)
        events = await ts.list(BillingUsageEvent,
                               BillingUsageEvent.capability_id == "prospector.search_rows")
    assert sum(e.quantity for e in events) == len(CONTACTS)     # bought once
    assert len(vendor.requests) == 1


async def test_the_daily_budget_refuses_with_429(client, vendor, monkeypatch):
    monkeypatch.setattr(get_settings(), "prospector_daily_rows_default", 100)
    h, tid = await _workspace(client, "ps13")
    from nexus.prospector.limits import count_rows

    async with tenant_session(tid) as ts:
        await count_rows(ts, 90)
    r = await _search(client, h, page_size=25)
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_an_invalid_filter_for_the_view_is_a_422(client, vendor):
    h, _ = await _workspace(client, "ps14")
    r = await client.post("/api/prospector/search", headers=h,
                          json={"view": "companies", "filters": {"titles": ["CFO"]}})
    assert r.status_code == 422 and vendor.requests == []


async def test_a_switched_off_module_refuses_with_its_message(client, vendor):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    h, _ = await _workspace(client, "ps15")
    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.prospector", state="maintenance",
                            message="Back at 14:00"))
        await s.commit()
    invalidate()
    r = await _search(client, h)
    assert r.status_code == 402 and vendor.requests == []


async def test_a_rep_may_search(client, vendor):
    h, _ = await _workspace(client, "ps16")
    r = await client.post("/api/workspace/members", headers=h, json={
        "email": "rep@ps16.io", "full_name": "Rep", "password": "password123", "role": "rep"})
    assert r.status_code == 201, r.text
    login = await client.post("/api/auth/login", json={"email": "rep@ps16.io",
                                                        "password": "password123"})
    rep = auth(login.json()["access_token"])
    assert (await _search(client, rep)).status_code == 200


async def test_the_filter_catalog_is_served(client):
    h, _ = await _workspace(client, "ps17")
    r = await client.get("/api/prospector/filters?view=companies", headers=h)
    assert r.status_code == 200
    keys = [s["key"] for s in r.json()["sections"]]
    assert keys[0] == "company" and "linkedin" in keys and "location" not in keys
