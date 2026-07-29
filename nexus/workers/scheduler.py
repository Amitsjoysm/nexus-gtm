"""Continuous Automation heartbeat.

A periodic coroutine that runs alongside the pull-only worker loop. Each tick, while the
global ``automation_enabled`` switch is on, it enqueues the recurring driver jobs
(``advance_cadences`` + ``refresh_due_accounts``). Both drivers are idempotent and
self-filtering, so enqueuing them every tick is safe and needs no per-job bookkeeping.

Run as part of ``python -m nexus.workers.worker`` (see ``worker.py``).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from nexus.core.config import get_settings
from nexus.core.db import get_sessionmaker
from nexus.workers.queue import TaskQueue, get_task_queue
from nexus.workers.tasks import (
    enqueue_advance_cadences,
    enqueue_discover_icp_accounts,
    enqueue_refresh_due_accounts,
    enqueue_roll_billing_periods,
    enqueue_rollup_usage,
    enqueue_send_daily_digests,
    enqueue_sync_crm_due_accounts,
)

logger = logging.getLogger("nexus.workers.scheduler")

# Postgres advisory-lock key for the singleton scheduler. A stable arbitrary 63-bit int; the
# heartbeat holds it only for the brief enqueue burst each tick, so exactly one worker in a
# horizontally-scaled fleet enqueues the recurring drivers — otherwise N workers would each
# enqueue them, N-fold multiplying digest sends and (before the atomic claim below) LLM spend.
_SCHEDULER_LOCK_KEY = 0x4E455853  # "NEXS"


async def _enqueue_due(queue: TaskQueue) -> int:
    """Enqueue the recurring drivers for whichever switches are on. Returns the count enqueued.

    Runs under a per-tick advisory lock so only the leader worker enqueues (see module docstring).
    The cadence + account-refresh drivers gate on automation_enabled; the CRM sweep gates on its
    own crm_sync_enabled switch. Each handler re-checks its switch, so this is a pre-filter.

    Usage rollups and billing-period rolls are enqueued unconditionally (no pre-filter
    early-return above): billing accuracy is not an opt-in feature, so every workspace's usage
    must roll up and every elapsed period must close on every tick, automation switches or
    not."""
    settings = get_settings()

    async with get_sessionmaker()() as session:
        if not await _acquire_scheduler_lock(session):
            return 0  # another worker holds the scheduler lock this tick
        try:
            count = 0
            # Usage rollups must run for every workspace, not just automation opt-ins — placed
            # outside the automation_enabled/crm_sync_enabled gates below on purpose.
            await enqueue_rollup_usage(queue=queue)
            count += 1
            # Same reasoning: a billing period ends on the calendar, not when a workspace opts
            # into automation. The handler self-filters to subscriptions whose window elapsed.
            await enqueue_roll_billing_periods(queue=queue)
            count += 1
            if settings.automation_enabled:
                await enqueue_advance_cadences(queue=queue)
                await enqueue_refresh_due_accounts(queue=queue)
                # Digest rides the automation switch; its handler is idempotent per interval.
                await enqueue_send_daily_digests(queue=queue)
                # Daily ICP auto-discovery: the handler atomically claims each tenant's per-interval
                # slot (Tenant.icp_discovery_last_run_at, row-locked), so it can't double-run.
                await enqueue_discover_icp_accounts(queue=queue)
                count += 4
            if settings.crm_sync_enabled:
                await enqueue_sync_crm_due_accounts(queue=queue)
                count += 1
            return count
        finally:
            await _release_scheduler_lock(session)


async def _acquire_scheduler_lock(session) -> bool:
    """Try to become the scheduler leader for this tick.

    Postgres: ``pg_try_advisory_lock`` — non-blocking; returns False if another worker holds it.
    Any non-Postgres backend (SQLite/in-memory dev) is single-process by construction, so there is
    no second scheduler to coordinate with — always the leader."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    got = await session.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _SCHEDULER_LOCK_KEY}
    )
    return bool(got.scalar())


async def _release_scheduler_lock(session) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    try:
        await session.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": _SCHEDULER_LOCK_KEY}
        )
    except Exception:  # a failed unlock self-heals when the connection returns to the pool
        logger.warning("scheduler advisory unlock failed", exc_info=True)


async def run_scheduler(
    *, stop: asyncio.Event | None = None, queue: TaskQueue | None = None
) -> None:
    """Heartbeat loop: enqueue drivers each tick until ``stop`` is set. Inert (loops but
    enqueues nothing) while ``automation_enabled`` is off."""
    stop = stop or asyncio.Event()
    queue = queue or get_task_queue()
    logger.info("scheduler started")
    while not stop.is_set():
        try:
            await _enqueue_due(queue)
        except Exception:
            # The drivers are idempotent and re-enqueued every tick, so a failed beat
            # (queue blip) costs one interval — never the whole heartbeat.
            logger.exception("heartbeat tick failed; will retry next tick")
        try:
            # stop.wait() with a timeout makes shutdown prompt (no fixed sleep to drain).
            await asyncio.wait_for(stop.wait(), timeout=get_settings().automation_tick_interval_s)
        except asyncio.TimeoutError:
            pass  # tick elapsed; loop again
    logger.info("scheduler stopping")
