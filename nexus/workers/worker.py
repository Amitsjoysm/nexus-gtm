"""Worker entrypoint: pull jobs off the queue and dispatch them.

Run with: ``python -m nexus.workers.worker``
"""
from __future__ import annotations

import asyncio
import logging
import signal

from nexus.core.db import dispose_db, init_db
from nexus.workers.queue import get_task_queue
from nexus.workers.tasks import dispatch

logger = logging.getLogger("nexus.workers.worker")


async def run_worker(*, stop: asyncio.Event | None = None, poll_timeout: float = 1.0) -> None:
    """Process jobs until ``stop`` is set. With an in-memory queue, use the same event loop."""
    stop = stop or asyncio.Event()
    queue = get_task_queue()
    logger.info("worker started")
    while not stop.is_set():
        job = await queue.dequeue(timeout=poll_timeout)
        if job is None:
            continue
        result = await dispatch(job)
        logger.info("job %s -> %s", job.name, result.get("error") or "ok")
    logger.info("worker stopping")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: signal handlers unsupported in loop
            pass
    try:
        await run_worker(stop=stop)
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(_main())
