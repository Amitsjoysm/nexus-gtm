"""Bounded in-flight concurrency in ``run_worker``.

The defect this closes: ``run_worker`` dequeued one job, awaited ``dispatch`` to completion, and
only then looked for the next one. Measured directly by ``deploy/loadtest/bench_heartbeat.py``
probe [D] — 20 jobs x 1 s of sleep drained in 20.37 s, **effective concurrency 0.98**. Since
``process_account`` is ~99% await-on-network, that idled the event loop for ~15 s per account
while the platform needed 5.11 accounts/sec.

The cap is not a free parameter: every in-flight job holds a tenant-bound session for its whole
life, so more in flight than the connection pool can serve turns a throughput win into
``TooManyConnectionsError``. Hence the cap is *derived from the pool*, and these tests pin that.

Everything here runs on the in-memory queue with retry delays pinned so the tests are
deterministic. The concurrency assertions are barrier-based, never wall-clock — a timing
threshold on a loaded CI box is a flake generator.
"""
from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import get_settings
from nexus.workers.queue import InMemoryTaskQueue, Job


# --------------------------------------------------------------------------- deriving the cap

def test_the_cap_is_the_db_pool_minus_a_reserve():
    """The pool is shared: the scheduler, the state-metrics gauge and the dead-letter writer all
    need a connection while jobs are in flight. Handing every slot to jobs starves them."""
    from nexus.workers.worker import POOL_RESERVE, resolve_worker_concurrency

    assert resolve_worker_concurrency(pool_size=10, max_overflow=20) == 30 - POOL_RESERVE


def test_the_cap_never_drops_below_one():
    """A tiny pool must still process jobs, one at a time — never zero, which is a stalled
    worker that looks healthy."""
    from nexus.workers.worker import resolve_worker_concurrency

    assert resolve_worker_concurrency(pool_size=1, max_overflow=0) == 1
    assert resolve_worker_concurrency(pool_size=0, max_overflow=0) == 1


def test_the_cap_falls_back_to_the_configured_pool_when_not_given():
    from nexus.workers.worker import resolve_worker_concurrency

    assert resolve_worker_concurrency() >= 1


def test_an_operator_can_pin_the_cap_with_an_env_var(monkeypatch):
    """Kill switch, not a rollout flag: if concurrency turns out to hurt a provider, setting this
    to 1 restores the old serial behaviour without a code change."""
    from nexus.workers.worker import resolve_worker_concurrency

    monkeypatch.setenv("NEXUS_WORKER_MAX_CONCURRENCY", "1")
    assert resolve_worker_concurrency(pool_size=10, max_overflow=20) == 1


def test_an_unparseable_env_override_falls_back_rather_than_crashing_the_worker(monkeypatch):
    """A typo in a container env var must not stop the fleet from starting."""
    from nexus.workers.worker import POOL_RESERVE, resolve_worker_concurrency

    monkeypatch.setenv("NEXUS_WORKER_MAX_CONCURRENCY", "banana")
    assert resolve_worker_concurrency(pool_size=10, max_overflow=20) == 30 - POOL_RESERVE

    monkeypatch.setenv("NEXUS_WORKER_MAX_CONCURRENCY", "0")
    assert resolve_worker_concurrency(pool_size=10, max_overflow=20) == 30 - POOL_RESERVE


# ------------------------------------------------------------------------------- the loop runs
#                                                                                  jobs in parallel

@pytest.fixture
def instant_retries(monkeypatch):
    """Zero backoff so the worker loop retries without sleeping."""
    monkeypatch.setattr(get_settings(), "job_retry_base_delay_s", 0.0)
    monkeypatch.setattr(get_settings(), "job_retry_max_delay_s", 0.0)


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_jobs_run_at_the_same_time_instead_of_one_after_another(monkeypatch):
    """Acceptance: N jobs are in flight together. Serial code cannot get past 1.

    No wall-clock assertion — every handler parks on the same barrier, which only opens once all
    N have started. A serial loop never opens it and the wait_for below fails the test.
    """
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    n = 8
    all_started = asyncio.Event()
    in_flight = 0
    peak = 0

    async def parks(payload: dict) -> dict:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if in_flight >= n:
            all_started.set()
        try:
            await all_started.wait()
        finally:
            in_flight -= 1
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_park", parks)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    for i in range(n):
        await q.enqueue(Job(name="conc_park", payload={"i": i}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=n))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=10)
    finally:
        stop.set()
        all_started.set()  # release anything still parked so the worker can exit either way
        await asyncio.wait_for(task, timeout=10)

    assert peak == n


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_in_flight_jobs_never_exceed_the_cap(monkeypatch):
    """The whole point of *bounded*: each in-flight job holds a DB session, so an unbounded
    fan-out would exhaust the pool and start failing jobs that would otherwise have succeeded."""
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    cap, n = 3, 12
    in_flight = 0
    peak = 0
    completed = 0
    done = asyncio.Event()

    async def slow(payload: dict) -> dict:
        nonlocal in_flight, peak, completed
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
        finally:
            in_flight -= 1
            completed += 1
            if completed >= n:
                done.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_capped", slow)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    for i in range(n):
        await q.enqueue(Job(name="conc_capped", payload={"i": i}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=cap))
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert completed == n, "every job must still be processed exactly once"
    assert peak <= cap, f"{peak} jobs in flight against a cap of {cap}"
    assert peak == cap, "the cap was never reached — the pool is not actually being used"


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_concurrent_jobs_do_not_leak_each_others_tenant(monkeypatch):
    """The tenant is a ContextVar, and ``tenant_session`` sets it for the duration of a handler.

    Running jobs in one shared task would let tenant B's ``set_current_tenant`` overwrite tenant
    A's binding mid-handler — a cross-tenant read, which is the worst bug this codebase can have.
    Each job therefore gets its own task (and so its own copied context).
    """
    from nexus.core.tenancy import get_current_tenant, set_current_tenant
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    n = 4
    both_in = asyncio.Event()
    entered = 0
    seen: dict[str, str | None] = {}
    done = asyncio.Event()

    async def binds_a_tenant(payload: dict) -> dict:
        nonlocal entered
        tenant_id = payload["tenant_id"]
        set_current_tenant(tenant_id)
        entered += 1
        if entered >= n:
            both_in.set()
        await both_in.wait()  # hold every job open so the bindings genuinely overlap
        seen[tenant_id] = get_current_tenant()
        if len(seen) >= n:
            done.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_tenant", binds_a_tenant)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    tenants = [f"t{i}" for i in range(n)]
    for tenant_id in tenants:
        await q.enqueue(Job(name="conc_tenant", payload={"tenant_id": tenant_id}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=n))
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        stop.set()
        both_in.set()
        await asyncio.wait_for(task, timeout=10)

    assert seen == {t: t for t in tenants}
    assert get_current_tenant() is None, "a job's tenant escaped into the worker's own context"


# ------------------------------------------------------------------- durability under concurrency

@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_flaky_job_is_still_retried_to_completion_under_concurrency(
    monkeypatch, instant_retries, fresh_db
):
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    attempts: list[int] = []
    done = asyncio.Event()

    async def flaky(payload: dict) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        done.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_flaky", flaky)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="conc_flaky", payload={}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=4))
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert len(attempts) == 3


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_permanently_failing_jobs_dead_letter_exactly_once_each_under_concurrency(
    monkeypatch, instant_retries, fresh_db
):
    """Several doomed jobs failing at the same time must produce one dead letter *per job*, with
    its own payload — not a race that loses some or double-writes others."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    n = 4
    calls: list[str] = []

    async def always_fails(payload: dict) -> dict:
        calls.append(payload["account_id"])
        raise RuntimeError("permanently broken")

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_doomed", always_fails)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    for i in range(n):
        await q.enqueue(
            Job(name="conc_doomed", payload={"tenant_id": "t1", "account_id": f"a{i}"})
        )

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=n))
    try:
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(calls) >= 3 * n:
                break
        await asyncio.sleep(0.1)  # let the dead-letter writes land
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert len(calls) == 3 * n, "each job must be attempted exactly max_attempts times"

    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(DeadLetterJob))).all()
    assert len(rows) == n
    assert {r.payload["account_id"] for r in rows} == {f"a{i}" for i in range(n)}
    assert all(r.attempts == 3 for r in rows)


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_retry_scheduled_by_a_job_still_in_flight_at_shutdown_is_flushed_back(
    monkeypatch, fresh_db
):
    """The ordering guard for the concurrent shutdown path.

    A job mid-backoff has already left the queue, so ``flush_pending_retries`` puts it back on
    the way out. Under concurrency that flush has to happen *after* the last in-flight job has
    finished failing — flush first and a job that fails during the drain schedules a retry that
    nothing will ever flush, which is exactly the silent loss the whole durability layer exists
    to prevent.
    """
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    # A long backoff so the retry is definitely still asleep when the worker exits.
    monkeypatch.setattr(get_settings(), "job_retry_base_delay_s", 30.0)
    monkeypatch.setattr(get_settings(), "job_retry_max_delay_s", 30.0)

    running = asyncio.Event()
    release = asyncio.Event()

    async def fails_slowly(payload: dict) -> dict:
        running.set()
        await release.wait()
        raise RuntimeError("failed during shutdown")

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_late_failure", fails_slowly)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="conc_late_failure", payload={"tenant_id": "t1"}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=4))
    await asyncio.wait_for(running.wait(), timeout=10)

    stop.set()          # shutdown begins while the job is still in flight
    release.set()       # ...and only now does it fail
    await asyncio.wait_for(task, timeout=10)

    assert await q.depth() == 1, "the in-flight job's retry was dropped on shutdown"
    job = await q.dequeue(timeout=0)
    assert job is not None and job.name == "conc_late_failure"
    assert job.attempts == 1


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_jobs_to_finish(monkeypatch):
    """A deploy must not abandon work that is already running. ``run_worker`` returns only once
    every in-flight handler has completed."""
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    running = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def slow(payload: dict) -> dict:
        nonlocal finished
        running.set()
        await release.wait()
        finished = True
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_slow_shutdown", slow)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="conc_slow_shutdown", payload={}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=4))
    await asyncio.wait_for(running.wait(), timeout=10)
    stop.set()
    await asyncio.sleep(0.05)
    assert not task.done(), "worker returned while a job was still running"

    release.set()
    await asyncio.wait_for(task, timeout=10)
    assert finished


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_queue_outage_does_not_kill_the_pool(monkeypatch):
    """Every consumer shares one broken queue. The loop must back off and recover rather than
    exit, and must still be draining once the queue comes back."""
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.queue import TaskQueue
    from nexus.workers.worker import run_worker

    ran = asyncio.Event()

    async def fine(payload: dict) -> dict:
        ran.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "conc_after_outage", fine)

    class FlakyQueue(TaskQueue):
        def __init__(self) -> None:
            self.inner = InMemoryTaskQueue()
            self.failures = 0

        async def enqueue(self, job: Job) -> None:
            await self.inner.enqueue(job)

        async def dequeue(self, *, timeout: float | None = None):
            if self.failures < 3:
                self.failures += 1
                raise ConnectionError("valkey is down")
            return await self.inner.dequeue(timeout=timeout)

        async def depth(self) -> int | None:
            return await self.inner.depth()

    q = FlakyQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="conc_after_outage", payload={}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01, concurrency=2))
    try:
        await asyncio.wait_for(ran.wait(), timeout=15)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15)

    assert q.failures == 3
