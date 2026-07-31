# nexus/workers/state_metrics.py
"""State gauges, exported by the worker.

Counters answer "how often did X happen"; these answer "how bad is it right now" — queue depth,
dead letters piling up, how many workspaces are in dunning. Both are needed: a dead-letter
*counter* rising tells you jobs failed at some point, and the *gauge* tells you nobody has
triaged them.

**Why the worker and not the API.** The app runs uvicorn with 2 workers, so it needs
``PROMETHEUS_MULTIPROC_DIR`` to make its counters add up across processes. In that mode
``prometheus_client`` reads only the mmap files: gauges need a declared aggregation mode and
custom collectors are not read at all. The worker is a single process, so gauges there are simply
correct. It also happens to be the process that already holds the DB and queue connections these
numbers come from.

**Why a refresh loop and not a scrape-time collector.** A collector that queries Postgres on every
scrape hands anyone who can reach ``/metrics`` a way to make us run four aggregate queries as fast
as they can ask. A loop on a fixed interval costs the same whether one Prometheus scrapes it or
twenty do.

Every gauge is set from a query that succeeded. A failed query leaves the previous value in place
rather than writing 0 — a zero here reads as "the problem went away", which is the one thing it
must never say when the truth is "we could not look".
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("nexus.workers.state_metrics")

_GAUGES: dict[str, object] = {}
_SERVER_STARTED = False


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()):
    existing = _GAUGES.get(name)
    if existing is not None:
        return existing
    try:
        from prometheus_client import Gauge

        g = Gauge(name, doc, labelnames=labels)
    except Exception:
        logger.debug("gauge %s unavailable", name, exc_info=True)
        return None
    _GAUGES[name] = g
    return g


def _set(name: str, doc: str, value: float, labels: dict[str, str] | None = None) -> None:
    g = _gauge(name, doc, tuple(labels or ()))
    if g is None:
        return
    try:
        (g.labels(**labels) if labels else g).set(value)
    except Exception:
        logger.debug("gauge %s set failed", name, exc_info=True)


async def refresh_state_metrics() -> dict[str, float]:
    """Recompute every state gauge. Returns what was set, for tests and for logging.

    Never raises: each block is independent, so a broken query costs one gauge rather than all of
    them. The worker's job is to keep working.
    """
    out: dict[str, float] = {}
    for step in (_refresh_queue_depth, _refresh_dead_letters, _refresh_billing_state):
        try:
            out.update(await step())
        except Exception:
            logger.warning("state metric refresh failed in %s", step.__name__, exc_info=True)
    return out


async def _refresh_queue_depth() -> dict[str, float]:
    from nexus.workers.queue import get_task_queue

    depth = await get_task_queue().depth()
    if depth is None:
        return {}          # unmeasurable: leave the gauge absent rather than claim zero
    _set("nexus_queue_depth", "Jobs waiting on the task queue.", float(depth))
    return {"nexus_queue_depth": float(depth)}


async def _refresh_dead_letters() -> dict[str, float]:
    from sqlalchemy import func, select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob

    async with get_sessionmaker()() as session:
        # Un-replayed only. Counting replayed ones too would mean the number never falls, so
        # "somebody dealt with it" would be indistinguishable from "nobody did".
        pending = await session.scalar(
            select(func.count()).select_from(DeadLetterJob).where(
                DeadLetterJob.replayed_at.is_(None)
            )
        )
    value = float(pending or 0)
    _set("nexus_dead_letter_jobs", "Dead-lettered jobs awaiting triage.", value)
    return {"nexus_dead_letter_jobs": value}


async def _refresh_billing_state() -> dict[str, float]:
    """Subscriptions and invoices by status.

    ``subscriptions{status="past_due"}`` *is* the dunning queue depth — it is the state dunning
    escalates a tenant into, so deriving it from the same rows the dunning sweep reads keeps the
    two from disagreeing.
    """
    from sqlalchemy import func, select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingInvoice, BillingSubscription

    out: dict[str, float] = {}
    async with get_sessionmaker()() as session:
        for model, name, doc in (
            (BillingSubscription, "nexus_subscriptions",
             "Subscriptions by status. past_due is the dunning queue depth."),
            (BillingInvoice, "nexus_invoices", "Invoices by status."),
        ):
            rows = (
                await session.execute(
                    select(model.status, func.count()).group_by(model.status)
                )
            ).all()
            for status, count in rows:
                _set(name, doc, float(count), {"status": str(status)})
                out[f'{name}{{status="{status}"}}'] = float(count)
    return out


def serve_worker_metrics(port: int) -> bool:
    """Expose the worker's registry over HTTP. Returns whether it started.

    The worker serves no requests, so its counters and gauges are otherwise unreachable by a
    scraper — which is why ``workers/metrics.py`` has been a dict nobody could graph.
    """
    global _SERVER_STARTED
    if _SERVER_STARTED:
        return True
    import os

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        # In multiprocess mode this endpoint would serve an empty registry: metrics go to mmap
        # files instead, and gauges/collectors are not read back. Say so rather than serve zeros.
        logger.warning(
            "PROMETHEUS_MULTIPROC_DIR is set in the worker; unset it or worker metrics will be "
            "empty (it is only needed by the multi-process API container)"
        )
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
    except Exception:
        logger.warning("worker metrics server failed to start on port %s", port, exc_info=True)
        return False
    _SERVER_STARTED = True
    logger.info("worker metrics listening on :%s/metrics", port)
    return True


async def run_state_metrics(
    *, stop: asyncio.Event | None = None, interval_s: float = 30.0
) -> None:
    """Refresh the state gauges until ``stop`` is set.

    Runs on **every** worker, not only the scheduler leader: each worker has its own registry and
    its own scrape target, so a leader-only refresh would leave the rest reporting nothing.
    """
    stop = stop or asyncio.Event()
    while not stop.is_set():
        await refresh_state_metrics()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
