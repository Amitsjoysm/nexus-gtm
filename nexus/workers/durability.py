"""Job durability: retry with jittered exponential backoff, then a dead letter.

Before M11 a handler exception was caught by ``dispatch``, logged, and dropped. The periodic
sweeps survived that because the scheduler re-enqueues them every tick and they are idempotent —
which is exactly why nobody noticed. The one-shot jobs (``process_account``, campaign sends,
orchestration runs) had no such second chance: one transient upstream 503 and the work was gone,
silently, with the customer left looking at an account that never got processed.

The contract here is "never drop it":
  * a failure that still has attempts left goes back on the SAME queue after a jittered backoff;
  * a failure that has exhausted them is parked in ``dead_letter_jobs`` with its payload intact,
    so an operator can fix the cause and replay it;
  * even a failure of the dead-letter WRITE is logged at ERROR with the whole payload, so the
    job is recoverable from the log when the database is the thing that is broken.

Handlers stay completely ignorant of all of this — no ``handle_*`` signature changes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random

from nexus.core.config import get_settings
from nexus.workers.metrics import increment_job_counter
from nexus.workers.queue import Job, TaskQueue

logger = logging.getLogger("nexus.workers.durability")

# ±25% dither. Without it, a provider outage that fails a thousand jobs at once retries all
# thousand at the same instant and re-DDoSes the service the moment it recovers.
_JITTER = 0.25

# Dead-letter `error` is operator-facing context, not a log sink: cap it so one pathological
# traceback cannot bloat the table.
_MAX_ERROR_CHARS = 4000

# Retries sleeping out their backoff. Held in a module-level registry for two reasons: an
# un-referenced asyncio Task can be garbage-collected mid-sleep (and the job with it), and a
# worker shutting down must be able to flush them back onto the queue instead of dropping them.
_pending_retries: dict[asyncio.Task, tuple[TaskQueue, Job]] = {}


def compute_backoff(attempts: int, *, base_s: float, max_s: float) -> float:
    """Delay before attempt ``attempts + 1``, jittered and capped.

    ``base_s <= 0`` means "immediately" — the test suite pins it there so the retry path is
    exercised without ever sleeping.
    """
    if base_s <= 0:
        return 0.0
    raw = min(base_s * (2 ** max(attempts - 1, 0)), max_s)
    jittered = raw * (1.0 + random.uniform(-_JITTER, _JITTER))
    return max(0.0, min(jittered, max_s))


async def record_job_failure(queue: TaskQueue, job: Job, error: str) -> str:
    """Account for one failed attempt. Returns ``"retried"`` or ``"dead_lettered"``.

    Mutates ``job.attempts`` — the count travels with the envelope, so the next worker to pick
    this job up knows how many chances are left even if this process is gone by then.
    """
    settings = get_settings()
    job.attempts += 1

    if settings.job_retry_enabled and job.attempts < job.max_attempts:
        delay = compute_backoff(
            job.attempts,
            base_s=settings.job_retry_base_delay_s,
            max_s=settings.job_retry_max_delay_s,
        )
        increment_job_counter("retried")
        logger.warning(
            "job %s failed (attempt %s/%s): %s — retrying in %.1fs",
            job.name, job.attempts, job.max_attempts, error, delay,
        )
        await _schedule_retry(queue, job, delay)
        return "retried"

    increment_job_counter("dead_lettered")
    logger.error(
        "job %s exhausted %s attempts: %s — dead-lettering payload %s",
        job.name, job.attempts, error, job.payload,
    )
    await _write_dead_letter(job, error)
    return "dead_lettered"


async def _schedule_retry(queue: TaskQueue, job: Job, delay: float) -> None:
    if delay <= 0:
        await _safe_enqueue(queue, job)
        return
    task = asyncio.create_task(_sleep_then_enqueue(queue, job, delay))
    _pending_retries[task] = (queue, job)
    # pop with a default: flush_pending_retries may have already cleared the registry by the
    # time the loop gets round to running this callback.
    task.add_done_callback(lambda t: _pending_retries.pop(t, None))


async def _sleep_then_enqueue(queue: TaskQueue, job: Job, delay: float) -> None:
    await asyncio.sleep(delay)
    await _safe_enqueue(queue, job)


async def _safe_enqueue(queue: TaskQueue, job: Job) -> None:
    """Re-enqueue, and if even that fails, dead-letter rather than drop."""
    try:
        await queue.enqueue(job)
    except Exception:
        logger.exception("re-enqueue failed for job %s; dead-lettering instead", job.name)
        await _write_dead_letter(job, "re-enqueue failed after a handler error")


async def flush_pending_retries() -> int:
    """Cancel outstanding backoff sleeps and put their jobs back on the queue NOW.

    Called on worker shutdown. A job mid-backoff has already been removed from the queue, so
    exiting without this would lose exactly the jobs that were already having a bad day.
    """
    if not _pending_retries:
        return 0
    outstanding = list(_pending_retries.items())
    for task, _ in outstanding:
        task.cancel()
    await asyncio.gather(*(t for t, _ in outstanding), return_exceptions=True)
    flushed = 0
    for task, (queue, job) in outstanding:
        if task.cancelled():
            await _safe_enqueue(queue, job)
            flushed += 1
    _pending_retries.clear()
    return flushed


def payload_digest(job_name: str, payload: dict) -> str:
    """Stable identity for "this same job failing again", so a recurring failure updates one row
    (first_seen_at / last_seen_at) instead of growing an unbounded pile of identical rows."""
    canonical = json.dumps({"name": job_name, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _write_dead_letter(job: Job, error: str) -> None:
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.jobs import DeadLetterJob

    payload = job.payload if isinstance(job.payload, dict) else {}
    digest = payload_digest(job.name, payload)
    # Named subject_tenant_id, never tenant_id: scripts/apply_rls.py enrols any table with a
    # `tenant_id` column into RLS, which would hide dead letters from the very operators whose
    # job is to read them (and from the worker writing them without a tenant binding).
    subject_tenant_id = payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None

    try:
        async with get_sessionmaker()() as session:
            existing = (
                await session.scalars(
                    select(DeadLetterJob).where(
                        DeadLetterJob.job_name == job.name,
                        DeadLetterJob.payload_digest == digest,
                        DeadLetterJob.replayed_at.is_(None),
                    )
                )
            ).first()
            now = utcnow()
            if existing is not None:
                existing.error = error[:_MAX_ERROR_CHARS]
                existing.attempts = job.attempts
                existing.last_seen_at = now
            else:
                session.add(
                    DeadLetterJob(
                        job_name=job.name,
                        payload=payload,
                        payload_digest=digest,
                        error=error[:_MAX_ERROR_CHARS],
                        attempts=job.attempts,
                        subject_tenant_id=subject_tenant_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            await session.commit()
    except Exception:
        # The database being unreachable must not kill the worker loop. Log the whole payload at
        # ERROR so the job is still recoverable by hand — this is the last line of defence.
        logger.exception(
            "dead-letter write FAILED for job %s; payload=%s error=%s", job.name, payload, error
        )
