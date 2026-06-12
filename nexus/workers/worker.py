"""Worker entrypoint: pull jobs off the queue and dispatch them.

Run with: ``python -m nexus.workers.worker``
"""
from __future__ import annotations

import asyncio
import logging
import signal

from nexus.core.db import dispose_db, init_db
from nexus.workers.queue import get_task_queue
from nexus.workers.scheduler import run_scheduler
from nexus.workers.tasks import dispatch

logger = logging.getLogger("nexus.workers.worker")


async def run_worker(*, stop: asyncio.Event | None = None, poll_timeout: float = 1.0) -> None:
    """Process jobs until ``stop`` is set. With an in-memory queue, use the same event loop.

    Infrastructure blips (a Valkey restart, a dropped connection) must not kill the loop:
    ``dispatch`` already contains handler errors, so anything escaping here is the queue
    itself — log it and retry with bounded backoff instead of crash-looping the container
    and pausing every periodic driver during the outage."""
    stop = stop or asyncio.Event()
    queue = get_task_queue()
    logger.info("worker started")
    backoff = 1.0
    while not stop.is_set():
        try:
            job = await queue.dequeue(timeout=poll_timeout)
        except Exception:
            logger.exception("queue unavailable; retrying in %.0fs", backoff)
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
        logger.info("job %s -> %s", job.name, result.get("error") or "ok")
    logger.info("worker stopping")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    from nexus.ingestion.crm_sync import register_crm_sync_subscribers

    register_crm_sync_subscribers()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: signal handlers unsupported in loop
            pass
    try:
        await asyncio.gather(
            run_worker(stop=stop),
            run_scheduler(stop=stop),
        )
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
