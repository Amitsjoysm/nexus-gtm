"""Continuous Automation (sub-project D): heartbeat scheduler + account refresh driver."""
from __future__ import annotations

import pytest

import importlib.util
from pathlib import Path

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
async def test_tenant_automation_flag_defaults_false():
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme", slug="acme-auto")
        s.add(t)
        await s.flush()
        assert t.automation_enabled is False


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


def test_migration_0008_chains_from_0007():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0008_continuous_automation.py"
    spec = importlib.util.spec_from_file_location("mig_0008", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0008_continuous_automation"
    assert mod.down_revision == "0007_cadence"
    assert hasattr(mod, "upgrade") and hasattr(mod, "downgrade")


from datetime import datetime, timedelta, timezone

from nexus.workers.queue import InMemoryTaskQueue, set_task_queue, get_task_queue
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
    assert count == 2
    jobs = await _drain(q)
    assert {j.name for j in jobs} == {"advance_cadences", "refresh_due_accounts"}


@pytest.mark.asyncio
async def test_enqueue_due_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    assert count == 0
    assert await _drain(q) == []


@pytest.mark.asyncio
async def test_run_scheduler_stops_promptly_when_event_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    stop = asyncio.Event()
    stop.set()  # already set → loop body never runs, returns immediately
    q = InMemoryTaskQueue()
    await asyncio.wait_for(run_scheduler(stop=stop, queue=q), timeout=1.0)
    assert await _drain(q) == []
