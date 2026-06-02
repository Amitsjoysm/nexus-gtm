"""Alerts: service CRUD, plays-engine wiring, and the list/ack API."""
from __future__ import annotations

from nexus.alerts.service import get_alert_service
from nexus.core.db import utcnow
from nexus.models.account import Account
from nexus.models.alerts import Alert
from nexus.models.signal import SignalEvent
from nexus.models.workflow import Play
from nexus.plays.engine import get_plays_engine
from tests.conftest import auth, make_tenant, signup, tenant_session


async def test_create_lists_and_acks_alert():
    tid = await make_tenant()
    svc = get_alert_service()
    async with tenant_session(tid) as ts:
        alert = await svc.create(ts, title="Funding round", severity="warning")
        assert alert.delivered_at is not None  # in_app delivers immediately

        opened = await svc.list(ts, status="open")
        assert [a.title for a in opened] == ["Funding round"]

        acked = await svc.acknowledge(ts, alert.id)
        assert acked.status == "acked" and acked.acked_at is not None
        assert await svc.list(ts, status="open") == []


async def test_invalid_severity_and_channel_are_normalized():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        a = await get_alert_service().create(
            ts, title="x", severity="bogus", channel="carrier-pigeon"
        )
        assert a.severity == "info" and a.channel == "in_app"


async def test_play_alert_action_persists_alert():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        sig = SignalEvent(
            tenant_id=tid, account_id=acc.id, kind="funding", source="t",
            title="Raised $20M", strength=0.9, dedupe_key="k", occurred_at=utcnow(),
        )
        ts.add(sig)
        await ts.flush()

        play = Play(
            tenant_id=tid, name="Funding alert", enabled=True,
            trigger={"signal_kinds": ["funding"]},
            actions=[{"type": "alert", "message": "Hot account", "severity": "critical"}],
        )
        ts.add(play)
        await ts.flush()

        runs = await get_plays_engine().evaluate(ts, account=acc, signal=sig, composite=80)
        assert len(runs) == 1

        alerts = await ts.list(Alert)
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert alerts[0].source == "play"
        assert alerts[0].account_id == acc.id


async def test_alerts_api_list_and_ack(client):
    token = await signup(client)
    h = auth(token)
    tid = (await client.post("/api/auth/login", json={
        "email": "rep@acme.com", "password": "password123"})).json()["tenant_id"]

    # Seed an alert directly so the endpoint has something to return.
    async with tenant_session(tid) as ts:
        a = await get_alert_service().create(ts, title="Seeded", severity="info")
        alert_id = a.id

    listed = await client.get("/api/alerts", headers=h)
    assert listed.status_code == 200
    assert any(x["id"] == alert_id for x in listed.json())

    acked = await client.post(f"/api/alerts/{alert_id}/ack", headers=h)
    assert acked.status_code == 200 and acked.json()["status"] == "acked"

    missing = await client.post("/api/alerts/does-not-exist/ack", headers=h)
    assert missing.status_code == 404
