"""Outbound CRM push: REST endpoint + plays ``crm_push`` action.

The connector records pushes in-memory (zero network), so the round-trip is provable offline:
we assert the recorded payload shape and that a failed/flaky CRM can never surface across the
boundary. Mirrors the SEP recording-connector tests.
"""
from __future__ import annotations

import pytest

from nexus.core.db import utcnow
from nexus.ingestion.crm import StubCRMConnector, set_crm_connector
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import Play
from nexus.plays.engine import get_plays_engine
from tests.conftest import auth, make_tenant, signup, tenant_session


@pytest.fixture(autouse=True)
def recording_crm():
    """Use a fresh recording CRM connector so pushes are observable and isolated."""
    connector = StubCRMConnector()
    set_crm_connector(connector)
    yield connector
    set_crm_connector(StubCRMConnector())


async def test_crm_push_writes_account_and_contacts(client, recording_crm):
    h = auth(await signup(client))
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com"})).json()
    await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h,
                      json={"full_name": "Jane Doe", "email": "jane@globex.com"})
    await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h,
                      json={"full_name": "John Roe", "email": "john@globex.com"})

    r = await client.post(f"/api/integrations/crm/push/{acc['id']}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["source"] == "stub"
    assert body["contacts"] == 2

    record = recording_crm.pushed_accounts[0]
    assert record["name"] == "Globex"
    assert record["domain"] == "globex.com"
    assert {c["full_name"] for c in record["contacts"]} == {"Jane Doe", "John Roe"}


async def test_crm_push_unknown_account_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/integrations/crm/push/nope", headers=h)
    assert r.status_code == 404


async def test_play_crm_push_action_records_account_and_activity():
    """A ``crm_push`` play action writes the account back and logs a signal activity."""
    connector = StubCRMConnector()
    set_crm_connector(connector)
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            acc = Account(tenant_id=tid, name="Acme", domain="acme.co", crm_id="CRM-1")
            ts.add(acc)
            await ts.flush()
            ts.add(Contact(tenant_id=tid, account_id=acc.id,
                           full_name="Ada Lovelace", email="ada@acme.co"))
            sig = SignalEvent(
                tenant_id=tid, account_id=acc.id, kind="funding", source="t",
                title="Raised $20M", strength=0.9, dedupe_key="k", occurred_at=utcnow(),
            )
            ts.add(sig)
            await ts.flush()

            play = Play(
                tenant_id=tid, name="Sync hot accounts", enabled=True,
                trigger={"signal_kinds": ["funding"]},
                actions=[{"type": "crm_push"}],
            )
            ts.add(play)
            await ts.flush()

            runs = await get_plays_engine().evaluate(
                ts, account=acc, signal=sig, composite=80)
            assert len(runs) == 1

        assert connector.pushed_accounts[0]["name"] == "Acme"
        assert connector.pushed_accounts[0]["contacts"][0]["full_name"] == "Ada Lovelace"
        activity = connector.pushed_activities[0]
        assert activity["kind"] == "signal"
        assert activity["account_id"] == "CRM-1"
        assert activity["detail"]["signal"] == "Raised $20M"
    finally:
        set_crm_connector(StubCRMConnector())
