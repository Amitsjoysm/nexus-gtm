"""Worker entrypoint: pull jobs off the queue and dispatch them.

Run with: ``python -m nexus.workers.worker``
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from nexus.core.config import get_settings
from nexus.core.db import dispose_db, init_db
from nexus.workers.durability import flush_pending_retries, record_job_failure
from nexus.workers.metrics import increment_job_counter
from nexus.workers.queue import TaskQueue, get_task_queue
from nexus.workers.scheduler import run_scheduler
from nexus.workers.tasks import dispatch, is_job_failure

logger = logging.getLogger("nexus.workers.worker")

# Connections held back from the job pool for the rest of this process, which still needs one
# while jobs are in flight: the scheduler's per-tick advisory lock, the state-metrics gauge
# sweep, and the dead-letter writer — which opens its OWN session at exactly the moment things
# are already going wrong, and is the last line of defence against losing a job.
POOL_RESERVE = 5

# Kill switch, and the escape hatch for a deployment whose Postgres is smaller than this
# process's pool config implies. Read straight from the environment rather than from Settings
# because the pool-sizing fields it derives from are themselves still in flight in
# ``nexus/core/config.py``; this belongs in Settings once that lands.
_CONCURRENCY_ENV = "NEXUS_WORKER_MAX_CONCURRENCY"


def _env_override() -> int | None:
    raw = os.environ.get(_CONCURRENCY_ENV)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the pool-derived cap", _CONCURRENCY_ENV, raw)
        return None
    if value < 1:
        logger.warning("%s=%s is below 1; using the pool-derived cap", _CONCURRENCY_ENV, value)
        return None
    return value


def resolve_worker_concurrency(
    *, pool_size: int | None = None, max_overflow: int | None = None
) -> int:
    """How many jobs this process may have in flight at once.

    Derived from the DB pool rather than chosen, because that is the real constraint: every
    handler runs inside ``tasks.tenant_session``, so an in-flight job holds a connection for its
    whole life. Fan out wider than the pool and the surplus jobs just block on ``pool.acquire``;
    past ``max_overflow`` they fail with ``TooManyConnectionsError`` — turning a throughput win
    into failures for jobs that would otherwise have succeeded. Concurrency here is bounded by
    connections, not by CPU: ``process_account`` is ~99% await-on-network.
    """
    override = _env_override()
    if override is not None:
        return override
    if pool_size is None or max_overflow is None:
        settings = get_settings()
        # getattr with the engine's own defaults: ``db_pool_size``/``db_max_overflow`` are part
        # of the in-flight pool-sizing change, and the worker has to start on a build predating
        # it rather than crash on an AttributeError at import.
        if pool_size is None:
            pool_size = getattr(settings, "db_pool_size", 10)
        if max_overflow is None:
            max_overflow = getattr(settings, "db_max_overflow", 20)
    return max(1, pool_size + max_overflow - POOL_RESERVE)


async def _consume(
    queue: TaskQueue, stop: asyncio.Event, poll_timeout: float, *, slot: int
) -> None:
    """One consumer: dequeue, dispatch, account for the outcome. Repeat until ``stop``.

    This is the loop the worker has always run; ``run_worker`` now runs N of them at once. Each
    is its own Task, and that is load-bearing for tenancy: asyncio copies the current context
    into a new task, so one handler's ``set_current_tenant`` cannot be observed by a job running
    beside it. Sharing one task between jobs would make a cross-tenant read possible.

    Infrastructure blips (a Valkey restart, a dropped connection) must not kill a consumer:
    ``dispatch`` contains handler errors, so anything escaping the dequeue is the queue itself —
    log it and retry with bounded backoff instead of crash-looping the container and pausing
    every periodic driver during the outage.

    A *handler* error is different, and since M11 it is no longer shrugged off: it is retried
    with backoff and finally dead-lettered (see ``nexus.workers.durability``). Dropping it was
    silent data loss for every one-shot job.
    """
    try:
        await _consume_forever(queue, stop, poll_timeout, slot=slot)
    except asyncio.CancelledError:
        raise
    except BaseException:
        # Anything escaping the loop below is a genuine bug — queue errors and handler errors are
        # both contained in there. Bring the whole pool down together and gracefully rather than
        # limping on with one slot fewer, and rather than leaving `gather` waiting forever on the
        # consumers that are still healthy.
        stop.set()
        raise


async def _consume_forever(
    queue: TaskQueue, stop: asyncio.Event, poll_timeout: float, *, slot: int
) -> None:
    backoff = 1.0
    while not stop.is_set():
        try:
            job = await queue.dequeue(timeout=poll_timeout)
        except Exception:
            # One outage is one incident, not N identical tracebacks. The first slot prints the
            # stack; the others say enough to show the whole pool is affected.
            if slot == 0:
                logger.exception("queue unavailable; retrying in %.0fs", backoff)
            else:
                logger.warning(
                    "queue unavailable (slot %s); retrying in %.0fs", slot, backoff
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
            continue
        backoff = 1.0
        if job is None:
            continue
        result = await dispatch(job)
        if is_job_failure(result):
            outcome = await record_job_failure(queue, job, str(result.get("error") or ""))
            logger.info("job %s -> %s", job.name, outcome)
        else:
            increment_job_counter("succeeded", job=job.name)
            logger.info("job %s -> %s", job.name, result.get("error") or "ok")


async def run_worker(
    *,
    stop: asyncio.Event | None = None,
    poll_timeout: float = 1.0,
    concurrency: int | None = None,
) -> None:
    """Process jobs until ``stop`` is set, up to ``concurrency`` of them at a time.

    Until M-perf this dequeued one job and awaited it to completion before looking for the next,
    which measured at an effective concurrency of 0.98 — one account-processing slot for the
    whole platform, against ~15.65 s of almost pure network wait per account. The cap defaults
    to :func:`resolve_worker_concurrency`; pass it explicitly only from tests and benchmarks.

    Shutdown is a graceful drain: every consumer finishes the job it is holding before returning.
    """
    stop = stop or asyncio.Event()
    queue = get_task_queue()
    limit = max(1, concurrency if concurrency is not None else resolve_worker_concurrency())
    logger.info("worker started (up to %s jobs in flight)", limit)
    consumers = [
        asyncio.create_task(
            _consume(queue, stop, poll_timeout, slot=i), name=f"nexus-worker-{i}"
        )
        for i in range(limit)
    ]
    try:
        # return_exceptions so one consumer hitting a genuine bug does not leave the others
        # orphaned mid-job; the failure is re-raised below, after the drain and the flush, so
        # run_worker still fails loudly the way the serial loop did.
        results = await asyncio.gather(*consumers, return_exceptions=True)
    finally:
        # Ordering here is load-bearing. Every consumer has returned by this point, so every
        # in-flight job has finished failing and registered whatever retry it scheduled. Jobs
        # asleep in a backoff have already left the queue; put them back before exiting, or a
        # deploy during an incident would lose exactly the jobs that were retrying. Flushing
        # *before* the drain misses every retry scheduled during it — the same silent loss,
        # reintroduced through the back door. There is a test that fails if this moves.
        flushed = await flush_pending_retries()
        if flushed:
            logger.info("flushed %s pending retries back onto the queue", flushed)
    logger.info("worker stopping")
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    from nexus.ingestion.crm_sync import register_crm_sync_subscribers
    from nexus.workers.state_metrics import run_state_metrics, serve_worker_metrics

    register_crm_sync_subscribers()
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: signal handlers unsupported in loop
            pass

    coros = [run_worker(stop=stop), run_scheduler(stop=stop)]
    # The worker serves no HTTP, so without this its job counters and state gauges are unreachable
    # by any scraper — "are jobs failing?" stays a question you answer by grepping logs.
    if (
        settings.metrics_enabled
        and settings.worker_metrics_port > 0
        and serve_worker_metrics(settings.worker_metrics_port)
    ):
        coros.append(
            run_state_metrics(stop=stop, interval_s=settings.worker_metrics_interval_s)
        )
    try:
        await asyncio.gather(*coros)
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
