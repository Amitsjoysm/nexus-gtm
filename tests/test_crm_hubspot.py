"""Real HubSpot CRM connector — offline (HTTP layer mocked; no network).

Asserts the outbound contract: companies upsert by domain/stored-id (no duplicate on re-sync),
contacts upsert by email + associate to the company, and that a CRM/API error degrades to a
recorded failure rather than raising across the boundary.
"""
from __future__ import annotations

from nexus.ingestion.crm import HubSpotConnector
from nexus.models.account import Account, Contact


def _account(**kw) -> Account:
    acc = Account(tenant_id="t", name=kw.pop("name", "Acme"), **kw)
    acc.id = "acc1"
    return acc


def _fake_request(routes: dict, calls: list):
    async def fake(method, path, body=None):
        calls.append((method, path))
        for key, resp in routes.items():
            if key in path:
                return resp
        return 200, {}
    return fake


async def test_push_creates_company_and_upserts_contacts():
    calls: list = []
    conn = HubSpotConnector(access_token="x")
    conn._request = _fake_request(
        {
            "companies/search": (200, {"results": []}),       # not found -> create
            "/crm/v3/objects/companies": (201, {"id": "555"}),
            "contacts/batch/upsert": (200, {"results": [{"id": "999"}]}),
            "/associations/default/": (200, {}),
        },
        calls,
    )
    acc = _account(domain="acme.co", industry="Software", employee_count=200)
    c = Contact(tenant_id="t", account_id="acc1", full_name="Jane Doe", title="VP", email="jane@acme.co")
    res = await conn.push_account(acc, contacts=[c])

    assert res.ok and res.external_id == "555"
    assert res.detail["contacts_synced"] == 1 and res.detail["action"] == "created"
    assert any(p == "/crm/v3/objects/companies" and m == "POST" for m, p in calls)
    assert any("contacts/batch/upsert" in p for _, p in calls)
    assert any("/associations/default/companies/555" in p for _, p in calls)


async def test_push_updates_in_place_when_crm_id_known():
    """A re-sync of an account we already pushed must PATCH the known company, never search/create."""
    calls: list = []
    conn = HubSpotConnector(access_token="x")
    conn._request = _fake_request({"/crm/v3/objects/companies/123": (200, {"id": "123"})}, calls)
    acc = _account(domain="acme.co")
    acc.crm_id = "123"
    acc.crm_source = "hubspot"
    res = await conn.push_account(acc, contacts=[])

    assert res.ok and res.external_id == "123" and res.detail["action"] == "updated"
    assert any(m == "PATCH" and "/companies/123" in p for m, p in calls)
    assert not any("search" in p for _, p in calls)  # no duplicate-creating search


async def test_push_degrades_to_failure_never_raises():
    calls: list = []
    conn = HubSpotConnector(access_token="x")
    conn._request = _fake_request(
        {"companies/search": (200, {"results": []}), "/crm/v3/objects/companies": (500, {"message": "boom"})},
        calls,
    )
    res = await conn.push_account(_account(domain="acme.co"), contacts=[])
    assert res.ok is False and "company upsert failed" in res.detail.get("error", "")


async def test_no_token_is_a_clean_failure():
    res = await HubSpotConnector(access_token="").push_account(_account(), contacts=[])
    assert res.ok is False and "no access token" in res.detail.get("error", "")
