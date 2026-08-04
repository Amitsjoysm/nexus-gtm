"""Continuous Automation (sub-project D): heartbeat scheduler + account refresh driver."""
from __future__ import annotations

import pytest


from nexus.core.config import get_settings
from nexus.core.db import get_sessionmaker
from nexus.models.identity import Tenant
from nexus.models.account import Account


def test_automation_config_defaults():
    s = get_settings()
    assert s.automation_enabled is False           # master switch OFF by default
    assert s.automation_tick_interval_s == 60
    assert s.account_refresh_interval_s == 21600    # 6h
    assert s.account_refresh_batch_size == 100


@pytest.mark.asyncio
async def test_tenant_automation_flag_defaults_on():
    """Flipped deliberately: it defaulted to False, which meant a brand-new workspace collected
    NOTHING until somebody found the toggle in Settings — observed as "zero signals after 30
    minutes" on a live deployment. A GTM tool whose premise is signal->action must not ship inert.

    What makes this affordable is the per-tenant daily crawl budget
    (`tenant_daily_source_runs`), not a switch nobody flips."""
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme", slug="acme-auto")
        s.add(t)
        await s.flush()
        assert t.automation_enabled is True


@pytest.mark.asyncio
async def test_account_last_refreshed_at_defaults_none():
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme3", slug="acme-auto3")
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Beta Corp", domain="beta.example")
        s.add(acct)
        await s.flush()
        assert acct.last_refreshed_at is None


# `test_migration_0008_chains_from_0007` lived here; revisions 0001-0020 are now squashed into
# the frozen `0020_baseline_schema`. tests/test_migrations_replay.py supersedes it by replaying
# the whole chain onto an empty database and diffing the result against Base.metadata.


from datetime import datetime, timedelta, timezone

from nexus.workers.queue import InMemoryTaskQueue, set_task_queue
from nexus.workers.tasks import handle_refresh_due_accounts


async def _drain(queue) -> list:
    jobs = []
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        jobs.append(job)
    return jobs


@pytest.mark.asyncio
async def test_refresh_selects_only_stale_in_opted_in_tenants(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=7)   # older than 6h interval → due
    fresh = now - timedelta(hours=1)   # within interval → not due

    async with get_sessionmaker()() as s:
        t = Tenant(name="Opted", slug="opted", automation_enabled=True)
        s.add(t)
        await s.flush()
        never = Account(tenant_id=t.id, name="Never", domain="never.x", last_refreshed_at=None)
        old = Account(tenant_id=t.id, name="Old", domain="old.x", last_refreshed_at=stale)
        recent = Account(tenant_id=t.id, name="Recent", domain="recent.x", last_refreshed_at=fresh)
        s.add_all([never, old, recent])
        await s.commit()
        never_id, old_id, recent_id = never.id, old.id, recent.id

    res = await handle_refresh_due_accounts({"now_iso": now.isoformat()})

    assert res["accounts"] == 2
    jobs = await _drain(q)
    enqueued_ids = {j.payload["account_id"] for j in jobs}
    assert all(j.name == "process_account" for j in jobs)
    assert enqueued_ids == {never_id, old_id}      # recent excluded

    # claimed accounts were stamped to `now`
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, never_id)).last_refreshed_at == now
        assert (await s.get(Account, old_id)).last_refreshed_at == now
        assert (await s.get(Account, recent_id)).last_refreshed_at == fresh
    set_task_queue(None)


@pytest.mark.asyncio
async def test_refresh_is_idempotent_within_interval(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Idem", slug="idem", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", last_refreshed_at=None))
        await s.commit()

    first = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert first["accounts"] == 1
    await _drain(q)
    # same `now` → the just-claimed account is no longer due
    second = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert second["accounts"] == 0
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_refresh_skips_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Off", slug="off", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", last_refreshed_at=None))
        await s.commit()
    res = await handle_refresh_due_accounts({})
    assert res == {"skipped": "automation_disabled"}
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_refresh_is_tenant_isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        opted = Tenant(name="In", slug="in-iso", automation_enabled=True)
        not_opted = Tenant(name="Out", slug="out-iso", automation_enabled=False)
        s.add_all([opted, not_opted])
        await s.flush()
        a_in = Account(tenant_id=opted.id, name="In", domain="in.x", last_refreshed_at=None)
        a_out = Account(tenant_id=not_opted.id, name="Out", domain="out.x", last_refreshed_at=None)
        s.add_all([a_in, a_out])
        await s.commit()
        in_id, out_id = a_in.id, a_out.id

    res = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert res["accounts"] == 1
    jobs = await _drain(q)
    assert {j.payload["account_id"] for j in jobs} == {in_id}

    async with get_sessionmaker()() as s:
        assert (await s.get(Account, out_id)).last_refreshed_at is None  # untouched
    set_task_queue(None)


import asyncio

from nexus.workers.scheduler import _enqueue_due, run_scheduler


@pytest.mark.asyncio
async def test_enqueue_due_enqueues_both_drivers_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    # +6 for rollup_usage, roll_billing_periods, dunning_sweep, billing_reconcile,
    # backfill_companies and crawl_companies — all enqueued every tick regardless of
    # automation_enabled, because billing accuracy and shared-company maintenance are platform
    # concerns rather than per-workspace opt-ins.
    assert count == 12
    jobs = await _drain(q)
    assert {j.name for j in jobs} == {
        "advance_cadences", "refresh_due_accounts", "send_daily_digests",
        "discover_icp_accounts", "rollup_usage", "roll_billing_periods", "dunning_sweep",
        "billing_reconcile", "expire_trials", "alert_digests", "backfill_companies", "crawl_companies",
    }


@pytest.mark.asyncio
async def test_enqueue_due_noop_when_disabled(monkeypatch):
    """With automation off, only the automation-gated drivers are skipped. Usage rollups,
    billing-period rolls, trial expiry and shared-company maintenance still run every tick — billing
    accuracy and the shared company store are platform concerns, not per-workspace opt-ins. A trial
    ends on a date; gating that on automation is how a trial runs forever in a workspace that never
    switched automation on."""
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    assert count == 8
    jobs = await _drain(q)
    assert {j.name for j in jobs} == {
        "rollup_usage", "roll_billing_periods", "dunning_sweep", "billing_reconcile",
        "expire_trials", "alert_digests", "backfill_companies", "crawl_companies",
    }


@pytest.mark.asyncio
async def test_enqueue_due_skips_when_not_scheduler_leader(monkeypatch):
    """C-1: a worker that does not hold the scheduler advisory lock enqueues nothing, so a
    horizontally-scaled fleet enqueues each driver once per tick instead of once per worker."""
    import nexus.workers.scheduler as sched

    monkeypatch.setattr(get_settings(), "automation_enabled", True)

    async def _not_leader(session):  # simulate another worker holding the lock this tick
        return False

    monkeypatch.setattr(sched, "_acquire_scheduler_lock", _not_leader)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    assert count == 0
    assert await _drain(q) == []  # follower enqueued nothing


@pytest.mark.asyncio
async def test_run_scheduler_stops_promptly_when_event_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    stop = asyncio.Event()
    stop.set()  # already set → loop body never runs, returns immediately
    q = InMemoryTaskQueue()
    await asyncio.wait_for(run_scheduler(stop=stop, queue=q), timeout=1.0)
    assert await _drain(q) == []


from nexus.workers.worker import run_worker


@pytest.mark.asyncio
async def test_worker_and_scheduler_stop_on_shared_event(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    stop = asyncio.Event()
    stop.set()  # both coroutines should observe this and return
    await asyncio.wait_for(
        asyncio.gather(
            run_worker(stop=stop, poll_timeout=0.1),
            run_scheduler(stop=stop, queue=q),
        ),
        timeout=2.0,
    )
    set_task_queue(None)


# ---- D-Task 8: automation toggle API ----

from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_automation_toggle_get_and_patch(client):
    token = await signup(client, slug="toggle", email="owner@toggle.x", company="Toggle")
    # Default is now ON. (The endpoint also returns icp_daily_count/icp_daily_default per commit
    # 0019; assert the specific field rather than exact dict equality so added fields don't break
    # it.) What this test protects is that the toggle round-trips in BOTH directions, so it now
    # switches off first — the direction a user actually needs when automation is on by default.
    r = await client.get("/api/workspace/automation", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["automation_enabled"] is True
    # turn off
    r = await client.patch(
        "/api/workspace/automation",
        headers=auth(token),
        json={"automation_enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["automation_enabled"] is False
    # ...and back on
    r = await client.patch(
        "/api/workspace/automation",
        headers=auth(token),
        json={"automation_enabled": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["automation_enabled"] is True
    # reads back on
    r = await client.get("/api/workspace/automation", headers=auth(token))
    assert r.json()["automation_enabled"] is True


@pytest.mark.asyncio
async def test_automation_toggle_isolated_between_tenants(client):
    a = await signup(client, slug="ta", email="a@ta.x", company="TenantA")
    b = await signup(client, slug="tb", email="b@tb.x", company="TenantB")
    # Both start ON (the new default), so isolation is now proved by A switching OFF: the setting
    # is per-tenant, and one workspace's change must never reach another's.
    await client.patch(
        "/api/workspace/automation", headers=auth(a), json={"automation_enabled": False}
    )
    r = await client.get("/api/workspace/automation", headers=auth(b))
    assert r.json()["automation_enabled"] is True   # B unaffected


@pytest.mark.asyncio
async def test_automation_toggle_forbidden_for_rep(client):
    owner = await signup(client, slug="rbac", email="owner@rbac.x", company="RbacCo")
    # invite a rep, then log in as that rep
    inv = await client.post(
        "/api/workspace/members",
        headers=auth(owner),
        json={
            "email": "rep@rbac.x",
            "full_name": "Rep",
            "password": "password123",
            "role": "rep",
        },
    )
    assert inv.status_code == 201, inv.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@rbac.x", "password": "password123"}
    )
    assert login.status_code == 200, login.text
    rep_token = login.json()["access_token"]

    r = await client.patch(
        "/api/workspace/automation",
        headers=auth(rep_token),
        json={"automation_enabled": True},
    )
    assert r.status_code == 403, r.text
