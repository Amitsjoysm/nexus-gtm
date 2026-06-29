"""Incident hardening: signup race -> 409, worker/scheduler survive queue outages."""
from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import get_settings
from nexus.workers.queue import InMemoryTaskQueue, Job, TaskQueue


@pytest.mark.asyncio
async def test_signup_unique_race_maps_to_409(client, monkeypatch):
    """The slug/email pre-checks are check-then-insert; the loser of a concurrent race hits
    the unique constraint at flush/commit. That path must surface as 409, never a 500.
    (Simulated deterministically: SQLite file locking makes true-parallel ASGI writes flaky.)"""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    body = {"company_name": "Race Co", "company_slug": "race-co",
            "full_name": "U1", "email": "u1@race-co.com", "password": "password123"}

    real_flush = AsyncSession.flush
    async def racing_flush(self, *a, **kw):
        # First flush of the request: behave as if another request inserted the slug
        # between our pre-check and our insert.
        monkeypatch.setattr(AsyncSession, "flush", real_flush)
        raise IntegrityError("INSERT INTO tenants", {}, Exception("UNIQUE constraint failed"))
    monkeypatch.setattr(AsyncSession, "flush", racing_flush)
    r = await client.post("/api/auth/signup", json=body)
    assert r.status_code == 409, r.text

    # And the untouched path still works end-to-end, then duplicates 409 via the pre-check.
    r2 = await client.post("/api/auth/signup", json=body)
    assert r2.status_code == 201, r2.text
    r3 = await client.post("/api/auth/signup", json={**body, "email": "u2@race-co.com"})
    assert r3.status_code == 409, r3.text


class _FlakyQueue(TaskQueue):
    """Raises like a dropped Valkey connection on the first N dequeues, then yields a job."""

    def __init__(self, failures: int):
        self.failures = failures
        self.dequeues = 0
        self.dispatched = asyncio.Event()

    async def enqueue(self, job: Job) -> None:  # pragma: no cover - unused
        raise AssertionError("not used")

    async def dequeue(self, *, timeout: float | None = None) -> Job | None:
        self.dequeues += 1
        if self.dequeues <= self.failures:
            raise ConnectionError("queue connection lost")
        self.dispatched.set()
        return None


# pytest-timeout's only Windows-compatible method ("thread") deadlocks against this test's own
# long-lived run_worker task; the test is already self-bounded by the wait_for(timeout=10) guards
# below, so opt it out of the global timeout (0 = disabled for this test).
@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_worker_loop_survives_queue_outage(monkeypatch):
    from nexus.workers import queue as queue_mod
    from nexus.workers.worker import run_worker

    flaky = _FlakyQueue(failures=2)
    monkeypatch.setattr(queue_mod, "_queue", flaky)
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01))
    # The loop must outlive the two connection errors and keep polling.
    await asyncio.wait_for(flaky.dispatched.wait(), timeout=10)
    stop.set()
    await asyncio.wait_for(task, timeout=10)  # returns cleanly, no exception
    assert flaky.dequeues >= 3


class _BrokenEnqueueQueue(InMemoryTaskQueue):
    async def enqueue(self, job: Job) -> None:
        raise ConnectionError("queue connection lost")


# Same as above: its own long-lived run_scheduler task is incompatible with the thread-based
# timeout watcher; the test self-bounds via sleep(0.1) + wait_for(timeout=10).
@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_scheduler_heartbeat_survives_enqueue_failure(monkeypatch):
    from nexus.workers.scheduler import run_scheduler

    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    monkeypatch.setattr(get_settings(), "automation_tick_interval_s", 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(stop=stop, queue=_BrokenEnqueueQueue()))
    await asyncio.sleep(0.1)  # several failing ticks elapse
    assert not task.done()    # heartbeat is still alive despite enqueue failures
    stop.set()
    await asyncio.wait_for(task, timeout=10)


def test_default_ingestion_excludes_demo_signals_when_disabled(monkeypatch):
    """Production must never fabricate signals: with NEXUS_DEMO_SIGNALS_ENABLED=false the
    default ingestion pipeline contains only real sources."""
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import DemoSignalSource

    monkeypatch.setattr(get_settings(), "demo_signals_enabled", False)
    set_ingestion_service(None)  # force re-composition from settings
    try:
        sources = get_ingestion_service().sources
        assert not any(isinstance(s, DemoSignalSource) for s in sources)
        assert len(sources) >= 1  # the real web source is still there

        monkeypatch.setattr(get_settings(), "demo_signals_enabled", True)
        set_ingestion_service(None)
        assert any(isinstance(s, DemoSignalSource) for s in get_ingestion_service().sources)
    finally:
        set_ingestion_service(None)


@pytest.mark.asyncio
async def test_metrics_off_by_default_and_app_serves(client):
    """Regression: metrics must be opt-in. Auto-enabling the instrumentator put a
    version-fragile middleware on every request — an incompatible FastAPI/instrumentator
    pair then 500'd logins. Default app: no /metrics, normal endpoints work."""
    assert get_settings().metrics_enabled is False
    r = await client.get("/api/health") if False else await client.get("/health")
    assert r.status_code == 200, r.text
    r = await client.get("/metrics")
    assert r.status_code == 404  # not mounted by default
