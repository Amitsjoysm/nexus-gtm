"""Taking records out of Prospector: the quote, and save / list / reveal / company list.

Pricing (decisions D4-D6): a contact costs 1 email credit when it has an email and 1 phone credit
per direct phone only when phones were asked for; a company from the Companies view costs 1
company credit; companies taken through the Contacts view are free; a field the workspace already
paid for is never charged again; a row the workspace already holds is free and untouched. The
balance is checked for the whole quote before anything is written, and records, list items, the
ledger and the charge land in one savepoint: all or nothing.
"""
from __future__ import annotations

import uuid

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.prospector_fixtures import COMPANIES, FakeVendor


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


@pytest.fixture(autouse=True)
async def _billing_seeded():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _workspace(client, slug: str) -> tuple[dict, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _rows(client, h, view="contacts", **body) -> list[dict]:
    r = await client.post("/api/prospector/search", headers=h,
                          json={"view": view, "page_size": 25, **body})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def _refs(rows):
    return [{"api_page": r["api_page"], "api_page_size": r["api_page_size"],
             "row_key": r["row_key"]} for r in rows]


def _selection(rows, view="contacts", **extra):
    return {"view": view, "scope": "total", "filters": {}, "rows": _refs(rows), **extra}


async def _quote(client, h, rows, *, view="contacts", action="save", phones=False):
    r = await client.post("/api/prospector/quote", headers=h, json={
        "selection": _selection(rows, view), "action": action, "include_phones": phones})
    assert r.status_code == 200, r.text
    return r.json()


async def _deliver(client, h, rows, *, view="contacts", action="save", phones=False,
                   list_id=None, request_id=None):
    return await client.post("/api/prospector/deliver", headers=h, json={
        "selection": _selection(rows, view), "action": action, "include_phones": phones,
        "list_id": list_id, "request_id": request_id or uuid.uuid4().hex})


async def _usage(tid: str) -> dict[str, float]:
    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id.in_(
            ["prospector.email", "prospector.phone", "prospector.company"]))
    out: dict[str, float] = {}
    for e in events:
        out[e.capability_id] = out.get(e.capability_id, 0) + float(e.quantity)
    return out


# ---- the quote ---------------------------------------------------------------------------------

async def test_a_contact_quote_charges_emails_and_phones_only_when_asked(client, vendor):
    h, _ = await _workspace(client, "pd1")
    rows = await _rows(client, h)

    without = await _quote(client, h, rows)
    assert without["charges"] == {"prospector.email": 3}
    assert without["total_credits"] == 3 and without["records"] == 4

    with_phones = await _quote(client, h, rows, phones=True)
    assert with_phones["charges"] == {"prospector.email": 3, "prospector.phone": 3}
    assert with_phones["total_credits"] == 6


async def test_a_held_row_is_free_and_counted(client, vendor):
    from nexus.models.account import Account, Contact

    h, tid = await _workspace(client, "pd2")
    async with tenant_session(tid) as ts:
        a = Account(name="Acme Example Inc", domain="acme-example.com")
        ts.add(a)
        await ts.flush()
        ts.add(Contact(account_id=a.id, full_name="John Smith", email="john.smith@acme-example.com"))
        await ts.flush()
    q = await _quote(client, h, await _rows(client, h))
    assert q["charges"] == {"prospector.email": 2} and q["held"] == 1


async def test_companies_cost_one_credit_each_and_through_contacts_nothing(client, vendor):
    h, _ = await _workspace(client, "pd3")
    companies = await _rows(client, h, view="companies")
    assert (await _quote(client, h, companies, view="companies"))["charges"] == \
        {"prospector.company": len(COMPANIES)}
    contacts = await _rows(client, h)
    assert (await _quote(client, h, contacts, action="company_list"))["charges"] == {}


# ---- save --------------------------------------------------------------------------------------

async def test_saving_contacts_creates_them_and_their_companies_once(client, vendor):
    from nexus.models.account import Account, Contact

    h, tid = await _workspace(client, "pd4")
    rows = await _rows(client, h)
    r = await _deliver(client, h, rows, phones=True)

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 4 and out["charged"] == {"prospector.email": 3, "prospector.phone": 3}
    async with tenant_session(tid) as ts:
        contacts = await ts.list(Contact)
        accounts = await ts.list(Account)
    assert len(contacts) == 4 and len(accounts) == 4
    john = next(c for c in contacts if c.full_name == "John Smith")
    assert john.email == "john.smith@acme-example.com" and john.phone == "+1 (415) 555-7890"
    assert john.enrichment_source == "sourcing:prospector" and john.email_status is None
    assert john.custom_fields["department"] == "Sales"
    assert john.custom_fields["location"]["country"] == "United States"
    acme = next(a for a in accounts if a.domain == "acme-example.com")
    assert acme.source == "prospector"
    assert await _usage(tid) == {"prospector.email": 3, "prospector.phone": 3}


async def test_phones_are_not_written_unless_bought(client, vendor):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "pd5")
    await _deliver(client, h, await _rows(client, h))
    async with tenant_session(tid) as ts:
        assert all(c.phone is None for c in await ts.list(Contact))


async def test_a_retry_creates_nothing_and_charges_nothing(client, vendor):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "pd6")
    rows = await _rows(client, h)
    key = uuid.uuid4().hex
    first = await _deliver(client, h, rows, request_id=key)
    again = await _deliver(client, h, rows, request_id=key)
    fresh = await _deliver(client, h, rows)                  # a new request for the same rows

    assert first.status_code == again.status_code == fresh.status_code == 200
    assert again.json()["created"] == 0 and again.json()["charged"] == {}
    assert fresh.json()["charged"] == {}
    async with tenant_session(tid) as ts:
        assert len(await ts.list(Contact)) == 4
    assert await _usage(tid) == {"prospector.email": 3}


async def test_a_balance_short_of_the_quote_writes_nothing(client, vendor, monkeypatch):
    from nexus.billing.credits import balance, burn_credits
    from nexus.models.account import Account, Contact
    from nexus.models.billing import BillingSubscription
    from nexus.models.prospector import ProspectorPurchase

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    h, tid = await _workspace(client, "pd7")
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = "launch"
        await ts.flush()
        left = await balance(ts)
        await burn_credits(ts, left - 2, reason="test", idempotency_key=uuid.uuid4().hex)

    r = await _deliver(client, h, await _rows(client, h))          # needs 3 credits

    assert r.status_code == 402, r.text
    async with tenant_session(tid) as ts:
        assert await ts.list(Contact) == [] and await ts.list(Account) == []
        assert await ts.list(ProspectorPurchase) == []
    assert await _usage(tid) == {}


# ---- lists -------------------------------------------------------------------------------------

async def test_listing_contacts_puts_them_on_a_contact_list(client, vendor):
    h, _ = await _workspace(client, "pd8")
    made = (await client.post("/api/lists", headers=h,
                              json={"name": "Cold", "kind": "contact"})).json()
    rows = await _rows(client, h)
    r = await _deliver(client, h, rows[:2], action="list", list_id=made["id"])
    assert r.status_code == 200, r.text
    assert r.json()["listed"] == 2
    members = (await client.get(f"/api/lists/{made['id']}/members", headers=h)).json()
    assert members["total"] == 2


async def test_a_list_of_the_wrong_kind_is_refused(client, vendor):
    h, _ = await _workspace(client, "pd9")
    accounts_list = (await client.post("/api/lists", headers=h,
                                       json={"name": "T1", "kind": "account"})).json()
    r = await _deliver(client, h, await _rows(client, h), action="list",
                       list_id=accounts_list["id"])
    assert r.status_code == 422


async def test_their_companies_go_on_an_account_list_for_free(client, vendor):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "pd10")
    tier = (await client.post("/api/lists", headers=h,
                              json={"name": "Track", "kind": "account"})).json()
    r = await _deliver(client, h, await _rows(client, h), action="company_list",
                       list_id=tier["id"])
    assert r.status_code == 200, r.text
    assert r.json()["listed"] == 4 and r.json()["charged"] == {}
    async with tenant_session(tid) as ts:
        assert await ts.list(Contact) == []
    assert await _usage(tid) == {}


# ---- reveal, companies, edge cases -------------------------------------------------------------

async def test_reveal_saves_one_row_and_the_search_then_shows_it_saved(client, vendor):
    h, _ = await _workspace(client, "pd11")
    rows = await _rows(client, h)
    r = await _deliver(client, h, rows[:1], action="reveal")
    assert r.status_code == 200, r.text
    assert len(r.json()["record_ids"]) == 1
    again = await _rows(client, h)
    assert again[0]["saved"] is True
    assert again[0]["record"]["email"] == "john.smith@acme-example.com"


async def test_reveal_takes_exactly_one_row(client, vendor):
    h, _ = await _workspace(client, "pd12")
    assert (await _deliver(client, h, (await _rows(client, h))[:2],
                           action="reveal")).status_code == 422


async def test_companies_are_saved_with_only_a_real_company_page(client, vendor):
    from nexus.models.account import Account

    vendor.companies = [dict(COMPANIES[0]),
                        {**COMPANIES[1], "CompanyLinkedin": "https://www.linkedin.com/in/someone"}]
    h, tid = await _workspace(client, "pd13")
    r = await _deliver(client, h, await _rows(client, h, view="companies"), view="companies")
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2 and r.json()["dropped_linkedin"] == 1
    async with tenant_session(tid) as ts:
        accounts = {a.domain: a for a in await ts.list(Account)}
    assert accounts["acme-example.com"].custom_fields["linkedin_url"] == \
        "https://www.linkedin.com/company/acme-example"
    assert "linkedin_url" not in accounts["globex-example.io"].custom_fields
    assert accounts["globex-example.io"].custom_fields["city"] == "New York"


async def test_an_existing_account_with_the_same_domain_is_reused(client, vendor):
    from nexus.models.account import Account

    h, tid = await _workspace(client, "pd14")
    async with tenant_session(tid) as ts:
        ts.add(Account(name="ACME (ours)", domain="www.acme-example.com"))
        await ts.flush()
    await _deliver(client, h, (await _rows(client, h))[:1])
    async with tenant_session(tid) as ts:
        assert len(await ts.list(Account, Account.name.like("%cme%"))) == 1


async def test_select_all_matching_takes_up_to_the_limit(client, vendor):
    h, tid = await _workspace(client, "pd15")
    r = await client.post("/api/prospector/deliver", headers=h, json={
        "selection": {"view": "contacts", "scope": "total", "filters": {},
                      "all_matching": {"limit": 2}},
        "action": "save", "request_id": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2


async def test_a_row_key_that_is_not_on_its_page_is_unavailable(client, vendor):
    h, _ = await _workspace(client, "pd16")
    rows = await _rows(client, h)
    bogus = [{**rows[0], "row_key": "0" * 32}]
    r = await _deliver(client, h, bogus)
    assert r.status_code == 200 and r.json()["unavailable"] == 1 and r.json()["created"] == 0


async def test_another_workspaces_row_keys_mean_nothing(client, vendor):
    h1, _ = await _workspace(client, "pd17a")
    h2, tid2 = await _workspace(client, "pd17b")
    rows = await _rows(client, h1)
    r = await _deliver(client, h2, rows)
    assert r.json()["unavailable"] == len(rows) and r.json()["created"] == 0
