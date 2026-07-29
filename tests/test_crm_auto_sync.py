"""CRM Auto-Sync (sub-project E): change-aware outbound sync via hybrid triggers."""
from __future__ import annotations


from nexus.core.config import get_settings


def test_crm_sync_config_defaults():
    s = get_settings()
    assert s.crm_sync_enabled is False        # master switch OFF by default
    assert s.crm_sync_batch_size == 100


import pytest

from nexus.core.db import get_sessionmaker
from nexus.models.account import Account
from nexus.models.identity import Tenant


@pytest.mark.asyncio
async def test_account_crm_synced_at_defaults_none():
    async with get_sessionmaker()() as s:
        t = Tenant(name="CrmCo", slug="crmco-default")
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Beta", domain="beta.crm")
        s.add(acct)
        await s.flush()
        assert acct.crm_synced_at is None


# `test_migration_0009_chains_from_0008` lived here; revisions 0001-0020 are now squashed into
# the frozen `0020_baseline_schema`. tests/test_migrations_replay.py supersedes it by replaying
# the whole chain onto an empty database and diffing the result against Base.metadata.


from datetime import datetime, timedelta, timezone

from nexus.ingestion.crm import StubCRMConnector
from nexus.models.account import Contact
from nexus.models.signal import SignalEvent
from nexus.workers.tasks import tenant_session


@pytest.mark.asyncio
async def test_sync_account_pushes_record_and_contacts_and_stamps():
    from nexus.ingestion.crm_sync import sync_account_to_crm

    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Push", slug="push-rec", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Acme", domain="acme.x", crm_synced_at=None)
        s.add(acct)
        await s.flush()
        s.add(Contact(tenant_id=t.id, account_id=acct.id, full_name="Jo Lead"))
        await s.commit()
        tid, aid = t.id, acct.id

    conn = StubCRMConnector()
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        res = await sync_account_to_crm(ts, acct, connector=conn, now=now)

    assert res.ok is True
    assert len(conn.pushed_accounts) == 1
    assert conn.pushed_accounts[0]["account_id"] == aid
    assert len(conn.pushed_accounts[0]["contacts"]) == 1
    # never-synced account: NO historical activity backfill on first sync
    assert conn.pushed_activities == []
    # crm_synced_at stamped to `now`
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, aid)).crm_synced_at == now


@pytest.mark.asyncio
async def test_sync_does_not_leave_account_perpetually_due():
    """Regression: stamping crm_synced_at is a row UPDATE, which must not let onupdate push
    updated_at *past* it — otherwise the account stays due and re-syncs every sweep (a scale
    bug at 1M accounts). After a sync, updated_at must be <= crm_synced_at."""
    from nexus.core.db import ensure_aware
    from nexus.ingestion.crm_sync import sync_account_to_crm

    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="NotDue", slug="sync-not-due", automation_enabled=True)
        s.add(t)
        await s.flush()
        # updated_at defaults to the real wall clock, which is well after the fixed `now`.
        acct = Account(tenant_id=t.id, name="Acme", domain="acme.x", crm_synced_at=None)
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    conn = StubCRMConnector()
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        await sync_account_to_crm(ts, acct, connector=conn, now=now)

    async with get_sessionmaker()() as s:
        a = await s.get(Account, aid)
        assert ensure_aware(a.updated_at) <= ensure_aware(a.crm_synced_at)


@pytest.mark.asyncio
async def test_sync_account_pushes_activity_for_signals_since_last_sync():
    from nexus.ingestion.crm_sync import sync_account_to_crm

    prior = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Act", slug="act-sig", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Acme2", domain="acme2.x", crm_synced_at=prior)
        s.add(acct)
        await s.flush()
        # one OLD signal (before last sync) and one NEW signal (after) — only NEW becomes activity
        s.add(SignalEvent(
            tenant_id=t.id, account_id=acct.id, kind="funding", source="news",
            title="Old round", dedupe_key="old-1", created_at=prior - timedelta(hours=2),
        ))
        s.add(SignalEvent(
            tenant_id=t.id, account_id=acct.id, kind="news", source="news",
            title="Fresh news", dedupe_key="new-1", created_at=now - timedelta(minutes=5),
        ))
        await s.commit()
        tid, aid = t.id, acct.id

    conn = StubCRMConnector()
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        await sync_account_to_crm(ts, acct, connector=conn, now=now)

    assert len(conn.pushed_accounts) == 1
    assert len(conn.pushed_activities) == 1          # only the post-last-sync signal
    act = conn.pushed_activities[0]
    assert act["kind"] == "signal"
    assert act["detail"]["signal"] == "Fresh news"


from nexus.ingestion.crm import set_crm_connector
from nexus.workers.queue import InMemoryTaskQueue, set_task_queue
from nexus.workers.tasks import (
    handle_sync_crm_account,
    handle_sync_crm_due_accounts,
)


async def _drain(queue) -> list:
    jobs = []
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        jobs.append(job)
    return jobs


@pytest.mark.asyncio
async def test_sweep_skips_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    res = await handle_sync_crm_due_accounts({})
    assert res == {"skipped": "crm_sync_disabled"}


@pytest.mark.asyncio
async def test_sweep_selects_only_due_accounts_in_opted_in_tenants(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    t0 = now - timedelta(hours=3)
    t1 = now - timedelta(hours=1)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Sweep", slug="sweep-due", automation_enabled=True)
        s.add(t)
        await s.flush()
        never = Account(tenant_id=t.id, name="Never", domain="n.x", crm_synced_at=None)
        changed = Account(tenant_id=t.id, name="Changed", domain="c.x",
                          crm_synced_at=t0, updated_at=t1)        # updated after sync → due
        fresh = Account(tenant_id=t.id, name="Fresh", domain="f.x",
                        crm_synced_at=t1, updated_at=t0)          # synced after update → not due
        s.add_all([never, changed, fresh])
        await s.commit()
        never_id, changed_id, fresh_id = never.id, changed.id, fresh.id

    res = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})

    assert res["accounts"] == 2
    pushed_ids = {r["account_id"] for r in conn.pushed_accounts}
    assert pushed_ids == {never_id, changed_id}      # fresh excluded
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, never_id)).crm_synced_at == now
        assert (await s.get(Account, changed_id)).crm_synced_at == now
        assert (await s.get(Account, fresh_id)).crm_synced_at == t1   # untouched
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_sweep_is_idempotent(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Idem", slug="sweep-idem", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", crm_synced_at=None))
        await s.commit()

    first = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert first["accounts"] == 1
    second = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert second["accounts"] == 0                  # already stamped → no longer due
    assert len(conn.pushed_accounts) == 1
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_sweep_is_tenant_isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        opted = Tenant(name="In", slug="sweep-in", automation_enabled=True)
        out = Tenant(name="Out", slug="sweep-out", automation_enabled=False)
        s.add_all([opted, out])
        await s.flush()
        a_in = Account(tenant_id=opted.id, name="In", domain="in.x", crm_synced_at=None)
        a_out = Account(tenant_id=out.id, name="Out", domain="out.x", crm_synced_at=None)
        s.add_all([a_in, a_out])
        await s.commit()
        in_id, out_id = a_in.id, a_out.id

    res = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert res["accounts"] == 1
    assert {r["account_id"] for r in conn.pushed_accounts} == {in_id}
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, out_id)).crm_synced_at is None  # untouched
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_single_account_handler_event_path(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="One", slug="one-acct", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Solo", domain="solo.x", crm_synced_at=None)
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    res = await handle_sync_crm_account({"tenant_id": tid, "account_id": aid})
    assert res == {"account_id": aid, "ok": True}
    assert {r["account_id"] for r in conn.pushed_accounts} == {aid}
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, aid)).crm_synced_at is not None
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_single_account_handler_respects_tenant_opt_out(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="OptOut", slug="opt-out", automation_enabled=False)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="NoSync", domain="ns.x", crm_synced_at=None)
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    res = await handle_sync_crm_account({"tenant_id": tid, "account_id": aid})
    assert res == {"skipped": "tenant_opted_out"}
    assert conn.pushed_accounts == []
    set_crm_connector(None)


from nexus.core.events import Event, EventBus
from nexus.ingestion.crm_sync import on_account_scored, register_crm_sync_subscribers


@pytest.mark.asyncio
async def test_on_account_scored_enqueues_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    await on_account_scored(
        Event(name="account.scored", tenant_id="t1", payload={"account_id": "a1"})
    )
    jobs = await _drain(q)
    assert len(jobs) == 1
    assert jobs[0].name == "sync_crm_account"
    assert jobs[0].payload == {"tenant_id": "t1", "account_id": "a1"}
    set_task_queue(None)


@pytest.mark.asyncio
async def test_on_account_scored_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    await on_account_scored(
        Event(name="account.scored", tenant_id="t1", payload={"account_id": "a1"})
    )
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_process_account_publishes_account_scored(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    bus = EventBus()                      # isolated bus for this test
    register_crm_sync_subscribers(bus)
    monkeypatch.setattr("nexus.pipeline.get_event_bus", lambda: bus)

    async with get_sessionmaker()() as s:
        t = Tenant(name="Pub", slug="pub-evt", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Pub", domain="pub.x")
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    from nexus.pipeline import process_account

    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        await process_account(ts, acct)

    jobs = await _drain(q)
    assert any(j.name == "sync_crm_account" and j.payload["account_id"] == aid for j in jobs)
    set_task_queue(None)


from nexus.workers.scheduler import _enqueue_due


@pytest.mark.asyncio
async def test_scheduler_enqueues_crm_sweep_when_crm_sync_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    await _enqueue_due(q)
    jobs = await _drain(q)
    # The billing drivers always ride along — they are not an automation/crm-sync opt-in.
    assert {j.name for j in jobs} == {
        "sync_crm_due_accounts", "rollup_usage", "roll_billing_periods",
    }


@pytest.mark.asyncio
async def test_scheduler_omits_crm_sweep_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    q = InMemoryTaskQueue()
    await _enqueue_due(q)
    jobs = await _drain(q)
    assert "sync_crm_due_accounts" not in {j.name for j in jobs}
    assert {j.name for j in jobs} == {
        "advance_cadences", "refresh_due_accounts", "send_daily_digests",
        "discover_icp_accounts", "rollup_usage", "roll_billing_periods",
    }


@pytest.mark.asyncio
async def test_app_lifespan_registers_account_scored_subscriber():
    from nexus.core.events import get_event_bus
    from nexus.main import create_app

    bus = get_event_bus()
    before = len(bus._handlers.get("account.scored", []))
    app = create_app()
    async with app.router.lifespan_context(app):
        after = len(bus._handlers.get("account.scored", []))
        assert after >= before + 1   # the CRM-sync subscriber is registered on startup


from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_crm_sync_status_reports_flags_and_pending(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    token = await signup(client, slug="cs", email="owner@cs.x", company="CsCo")
    # opt the tenant into automation so `enabled` composes True
    await client.patch(
        "/api/workspace/automation", headers=auth(token), json={"automation_enabled": True}
    )
    # create two accounts via inbound CRM sync (they land with crm_synced_at = NULL → pending)
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=auth(token),
        json={
            "source": "salesforce",
            "accounts": [
                {"external_id": "x1", "name": "One"},
                {"external_id": "x2", "name": "Two"},
            ],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/integrations/crm/sync-status", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["provider"] == "stub"
    assert body["pending"] == 2
    assert body["synced"] == 0


@pytest.mark.asyncio
async def test_crm_sync_status_forbidden_for_rep(client):
    owner = await signup(client, slug="csr", email="owner@csr.x", company="CsrCo")
    inv = await client.post(
        "/api/workspace/members",
        headers=auth(owner),
        json={"email": "rep@csr.x", "full_name": "Rep", "password": "password123", "role": "rep"},
    )
    assert inv.status_code == 201, inv.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@csr.x", "password": "password123"}
    )
    rep_token = login.json()["access_token"]
    r = await client.get("/api/integrations/crm/sync-status", headers=auth(rep_token))
    assert r.status_code == 403, r.text
