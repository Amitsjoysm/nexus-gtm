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
        # Honor the TaskQueue contract: a real empty queue blocks up to `timeout` before
        # returning None (see InMemoryTaskQueue/RedisTaskQueue.dequeue). Returning instantly
        # would let the worker's None-poll loop spin without ever yielding to the event loop,
        # starving this test's own wait_for guards.
        await asyncio.sleep(timeout or 0)
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


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_demo_signals_hard_disabled_in_production(monkeypatch, env):
    """Fail-safe: staging/prod must never fabricate signals, even if the operator leaves
    NEXUS_DEMO_SIGNALS_ENABLED=true. The env-based guard wins over the raw flag."""
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import DemoSignalSource

    settings = get_settings()
    monkeypatch.setattr(settings, "demo_signals_enabled", True)  # operator mistake
    monkeypatch.setattr(settings, "env", env)
    set_ingestion_service(None)  # force re-composition from settings
    try:
        assert settings.demo_signals_active is False
        sources = get_ingestion_service().sources
        assert not any(isinstance(s, DemoSignalSource) for s in sources)
        assert len(sources) >= 1  # the real web source is still there
    finally:
        set_ingestion_service(None)


@pytest.mark.asyncio
async def test_instrumentation_never_breaks_the_app(client):
    """The original incident, still guarded — but the guard has moved.

    Auto-enabling the instrumentator once put a version-fragile middleware on every request, and
    an incompatible FastAPI/instrumentator pair then 500'd logins. The fix at the time was to make
    metrics opt-in, and this test asserted that default.

    M15 flipped the default back to ON, because "observability is opt-in" in practice means nobody
    turned it on, and a deployment blind to queue lag, 402 rates and dunning depth is not operable.
    What actually prevents a repeat is not the default: it is that `_maybe_enable_metrics` wraps
    instrumentation so an incompatible install degrades to "no metrics", and that pyproject pins
    both sides against exactly that pair. So the invariant under test is the one that matters —
    **ordinary endpoints work with instrumentation active** — rather than the flag's value.
    """
    assert get_settings().metrics_enabled is True

    r = await client.get("/health")
    assert r.status_code == 200, r.text
    r = await client.get("/ready")
    assert r.status_code in (200, 503), r.text

    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "# HELP" in r.text or "# TYPE" in r.text


@pytest.mark.asyncio
async def test_a_broken_instrumentator_degrades_to_no_metrics(monkeypatch):
    """The real protection against the original incident: if instrumenting raises, the app must
    still be built and serve. Losing metrics is an inconvenience; 500ing every login is an
    outage."""
    import httpx

    import nexus.main as main

    def explode(_app):
        raise RuntimeError("instrumentator/FastAPI mismatch")

    # Patch the instrumentator call itself, not the wrapper, so the wrapper is what is tested.
    monkeypatch.setattr(main, "_instrument", explode, raising=False)

    app = main.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
        assert r.status_code == 200, r.text
