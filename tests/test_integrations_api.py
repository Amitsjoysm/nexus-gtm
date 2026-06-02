"""CRM sync and SEP push REST endpoints."""
from __future__ import annotations

import pytest

from nexus.integrations.sep import OutreachConnector, set_sep_connector
from tests.conftest import auth, signup


@pytest.fixture(autouse=True)
def recording_sep():
    """Use a fresh recording SEP connector so pushes are observable and isolated."""
    connector = OutreachConnector()
    set_sep_connector(connector)
    yield connector
    set_sep_connector(OutreachConnector())


async def test_crm_sync_upserts_accounts(client):
    h = auth(await signup(client))
    payload = {
        "source": "salesforce",
        "accounts": [
            {"external_id": "001", "name": "Globex", "domain": "globex.com",
             "industry": "Software", "employee_count": 500, "country": "US"},
            {"external_id": "002", "name": "Initech", "domain": "initech.com"},
        ],
    }
    r = await client.post("/api/integrations/crm/sync", headers=h, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["synced"] == 2

    # Idempotent: syncing the same external_ids updates, does not duplicate.
    again = await client.post("/api/integrations/crm/sync", headers=h, json=payload)
    assert again.json()["synced"] == 2

    accounts = (await client.get("/api/accounts", headers=h)).json()
    assert sorted(a["name"] for a in accounts) == ["Globex", "Initech"]


async def test_sep_push_records_contact(client, recording_sep):
    h = auth(await signup(client))
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com"})).json()
    contact = (await client.post(
        f"/api/accounts/{acc['id']}/contacts", headers=h,
        json={"full_name": "Jane Doe", "email": "jane@globex.com"})).json()

    r = await client.post("/api/integrations/sep/push", headers=h, json={
        "sequence": "enterprise", "contact_id": contact["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["platform"] == "outreach"
    assert recording_sep.pushed[0]["email"] == "jane@globex.com"
    assert recording_sep.pushed[0]["sequence"] == "enterprise"


async def test_sep_push_unknown_contact_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/integrations/sep/push", headers=h, json={
        "sequence": "x", "contact_id": "nope"})
    assert r.status_code == 404
