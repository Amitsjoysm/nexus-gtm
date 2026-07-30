# nexus/api/routers/admin_jobs.py
"""Staff-only dead-letter triage.

The other half of M11. Retry-with-backoff keeps a transient failure from losing work; this is
what happens when the failure was not transient — the job is parked with its payload, and an
operator who has fixed the cause replays it from here.

Gated on ``jobs.manage``: dead letters span every tenant, so no tenant role may see them, and
replaying someone's job is not something a support admin needs. Every replay is written to the
audit log — re-running a customer's job is an admin mutation, and "who re-ran this, and when" has
to come from a record.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import JOBS_MANAGE
from nexus.core.db import get_sessionmaker, utcnow
from nexus.models.jobs import DeadLetterJob
from nexus.workers.queue import Job, get_task_queue

logger = logging.getLogger("nexus.api.admin_jobs")

router = APIRouter(prefix="/admin/jobs", tags=["admin-jobs"])


class DeadLetterOut(BaseModel):
    id: str
    job_name: str
    payload: dict
    error: str
    attempts: int
    subject_tenant_id: str | None
    first_seen_at: str | None
    last_seen_at: str | None
    replayed_at: str | None


def _out(row: DeadLetterJob) -> DeadLetterOut:
    return DeadLetterOut(
        id=row.id,
        job_name=row.job_name,
        payload=row.payload or {},
        error=row.error or "",
        attempts=row.attempts,
        subject_tenant_id=row.subject_tenant_id,
        first_seen_at=row.first_seen_at.isoformat() if row.first_seen_at else None,
        last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else None,
        replayed_at=row.replayed_at.isoformat() if row.replayed_at else None,
    )


@router.get("/dead-letters", response_model=list[DeadLetterOut])
async def list_dead_letters(
    job_name: str | None = None,
    # Default to the OPEN ones: the list is a work queue for a human, and a replayed job is
    # done. History stays one query parameter away.
    include_replayed: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    _: Principal = Depends(require_platform_permission(JOBS_MANAGE)),
) -> list[DeadLetterOut]:
    stmt = select(DeadLetterJob)
    if job_name:
        stmt = stmt.where(DeadLetterJob.job_name == job_name)
    if not include_replayed:
        stmt = stmt.where(DeadLetterJob.replayed_at.is_(None))
    stmt = stmt.order_by(DeadLetterJob.last_seen_at.desc()).limit(limit)
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(stmt)).all()
    return [_out(r) for r in rows]


@router.post("/dead-letters/{dead_letter_id}/replay")
async def replay_dead_letter(
    dead_letter_id: str,
    principal: Principal = Depends(require_platform_permission(JOBS_MANAGE)),
) -> dict:
    """Put one dead-lettered job back on the queue with a fresh attempt budget.

    Marked replayed and committed *before* the enqueue, so a double-clicked button hits the 409
    below rather than running a side-effectful job twice. If the enqueue then fails, the mark is
    reverted so the job stays visible in triage — the one thing that must never happen is a row
    that reads "replayed" for work that was never scheduled.
    """
    async with get_sessionmaker()() as session:
        row = await session.get(DeadLetterJob, dead_letter_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown dead letter")
        if row.replayed_at is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Dead letter already replayed at {row.replayed_at.isoformat()}",
            )
        job_name = row.job_name
        payload = dict(row.payload or {})
        subject_tenant_id = row.subject_tenant_id
        row.replayed_at = utcnow()
        await record_admin_action(
            session,
            actor=principal.user_id,
            action="job.dead_letter.replay",
            target=dead_letter_id,
            subject_tenant_id=subject_tenant_id,
            before={"job_name": job_name, "attempts": row.attempts, "replayed_at": None},
            after={"job_name": job_name, "replayed_at": row.replayed_at.isoformat()},
            note=f"replayed {job_name}",
        )
        await session.commit()

    try:
        # attempts=0: a replay is a fresh start. Carrying the exhausted count over would mean a
        # job that dead-lettered once could not survive a single transient failure again.
        await get_task_queue().enqueue(Job(name=job_name, payload=payload))
    except Exception:
        logger.exception("replay enqueue failed for dead letter %s", dead_letter_id)
        async with get_sessionmaker()() as session:
            again = await session.get(DeadLetterJob, dead_letter_id)
            if again is not None:
                again.replayed_at = None
                await session.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "queue unavailable; replay was not scheduled"
        )

    return {"id": dead_letter_id, "job_name": job_name, "status": "replayed"}
