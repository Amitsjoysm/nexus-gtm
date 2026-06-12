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

from nexus.core.config import get_settings
from nexus.workers.queue import TaskQueue, get_task_queue
from nexus.workers.tasks import (
    enqueue_advance_cadences,
    enqueue_refresh_due_accounts,
    enqueue_send_daily_digests,
    enqueue_sync_crm_due_accounts,
)

logger = logging.getLogger("nexus.workers.scheduler")


async def _enqueue_due(queue: TaskQueue) -> int:
    """Enqueue the recurring drivers for whichever switches are on. Returns the count enqueued.

    The cadence + account-refresh drivers gate on automation_enabled; the CRM sweep gates on its
    own crm_sync_enabled switch (so it can run independently). Each handler re-checks its switch,
    so this is a pre-filter, not the authority."""
    settings = get_settings()
    count = 0
    if settings.automation_enabled:
        await enqueue_advance_cadences(queue=queue)
        await enqueue_refresh_due_accounts(queue=queue)
        # Digest rides the automation switch; its handler is idempotent per interval, so
        # enqueueing every tick costs one cheap timestamp check per tenant.
        await enqueue_send_daily_digests(queue=queue)
        count += 3
    if settings.crm_sync_enabled:
        await enqueue_sync_crm_due_accounts(queue=queue)
        count += 1
    return count


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
