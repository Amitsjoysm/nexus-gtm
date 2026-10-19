"""Exporting Prospector rows to CSV: charged like a delivery, once per field, and formula-safe.

The file is the paid delivery: it carries real values, so it is charged exactly as saving would be
(D3-D6), committed before the file is returned, and a re-download under a new request costs
nothing. Provider text is attacker-influenced, so every cell that would start a spreadsheet formula
is neutralised.
"""
from __future__ import annotations

import csv
import io
import uuid

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.prospector_fixtures import CONTACTS, FakeVendor


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


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _export(client, h, view="contacts", phones=False, request_id=None):
    rows = (await client.post("/api/prospector/search", headers=h,
                              json={"view": view, "page_size": 25})).json()["rows"]
    refs = [{"api_page": r["api_page"], "api_page_size": r["api_page_size"],
             "row_key": r["row_key"]} for r in rows]
    return await client.post("/api/prospector/export", headers=h, json={
        "selection": {"view": view, "scope": "total", "filters": {}, "rows": refs},
        "include_phones": phones, "request_id": request_id or uuid.uuid4().hex})


def _read(resp) -> list[dict]:
    text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


async def test_a_contact_export_carries_real_values_and_is_charged(client, vendor):
    from nexus.models.billing import BillingUsageEvent

    h, tid = await _workspace(client, "pe1")
    r = await _export(client, h)

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["X-Row-Count"] == str(len(CONTACTS))
    rows = _read(r)
    assert list(rows[0]) == ["first_name", "last_name", "title", "department", "seniority",
                             "email", "company_name", "company_domain", "country", "state", "city"]
    assert rows[0]["email"] == "john.smith@acme-example.com"
    assert rows[0]["company_domain"] == "acme-example.com"
    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent,
                               BillingUsageEvent.capability_id == "prospector.email")
    assert sum(e.quantity for e in events) == 3


async def test_phones_are_a_column_only_when_bought(client, vendor):
    h, _ = await _workspace(client, "pe2")
    rows = _read(await _export(client, h, phones=True))
    assert "direct_phone" in rows[0]
    # A leading "+" would start a formula in a spreadsheet: neutralised.
    assert rows[0]["direct_phone"] == "'+1 (415) 555-7890"


async def test_a_formula_in_provider_text_is_neutralised(client, vendor):
    vendor.contacts = [{**CONTACTS[0], "CompanyName": '=HYPERLINK("http://evil.example","x")'}]
    h, _ = await _workspace(client, "pe3")
    rows = _read(await _export(client, h))
    assert rows[0]["company_name"].startswith("'=HYPERLINK")


async def test_exporting_again_costs_nothing(client, vendor):
    from nexus.models.billing import BillingUsageEvent

    h, tid = await _workspace(client, "pe4")
    await _export(client, h)
    second = await _export(client, h)
    assert second.status_code == 200 and len(_read(second)) == len(CONTACTS)
    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent,
                               BillingUsageEvent.capability_id == "prospector.email")
    assert sum(e.quantity for e in events) == 3


async def test_a_short_balance_writes_nothing_and_returns_402(client, vendor, monkeypatch):
    from nexus.billing.credits import balance, burn_credits
    from nexus.models.billing import BillingSubscription
    from nexus.models.prospector import ProspectorPurchase

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    h, tid = await _workspace(client, "pe5")
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = "launch"
        await ts.flush()
        await burn_credits(ts, await balance(ts) - 1, reason="test",
                           idempotency_key=uuid.uuid4().hex)
    r = await _export(client, h)
    assert r.status_code == 402
    async with tenant_session(tid) as ts:
        assert await ts.list(ProspectorPurchase) == []


async def test_a_held_row_exports_the_workspaces_own_values_for_free(client, vendor):
    from nexus.models.account import Account, Contact

    h, tid = await _workspace(client, "pe6")
    async with tenant_session(tid) as ts:
        a = Account(name="Acme Example Inc", domain="acme-example.com")
        ts.add(a)
        await ts.flush()
        ts.add(Contact(account_id=a.id, full_name="John Smith",
                       email="john.smith@acme-example.com", title="Chief Revenue Officer"))
        await ts.flush()
    rows = _read(await _export(client, h))
    john = next(r for r in rows if r["last_name"] == "Smith")
    assert john["title"] == "Chief Revenue Officer"            # ours, not the provider's


async def test_a_company_export_has_its_columns(client, vendor):
    h, _ = await _workspace(client, "pe7")
    rows = _read(await _export(client, h, view="companies"))
    assert list(rows[0]) == ["company_name", "domain", "country", "state", "city", "industry",
                             "employee_size_range", "linkedin_url"]
    assert rows[0]["linkedin_url"] == "https://linkedin.com/company/acme-example"


def test_safe_cell_leaves_ordinary_values_alone():
    from nexus.api.csv_export import safe_cell

    assert safe_cell("Acme") == "Acme" and safe_cell(42) == 42 and safe_cell(None) == ""
    assert safe_cell(["a", "b"]) == "a; b"
    for lead in ("=", "+", "-", "@", "\t", "\r"):
        assert safe_cell(f"{lead}1") == f"'{lead}1"
