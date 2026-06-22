"""Call queue service: enqueue calls, generate AI scripts, log dispositions.

The queue is the SDR's prioritized call list; dispositions are logged as :class:`CallActivity`
rows (the call history + analytics source). Enqueue is idempotent (at most one OPEN task per
contact / per cadence step), mirroring the Inbox dedupe so an account/contact never piles up
duplicate calls. Cadence progression is decoupled: a cadence call-step queues a task and the
sequence advances on schedule (like an email send) — logging the outcome never blocks the cadence.
"""
from __future__ import annotations

from datetime import datetime

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.calling import (
    CALL_DONE,
    CALL_OPEN,
    REQUEUE_DISPOSITIONS,
    SOURCE_MANUAL,
    CallActivity,
    CallTask,
)


class CallQueueService:
    async def enqueue(
        self,
        ts: TenantSession,
        *,
        account_id: str,
        contact_id: str | None = None,
        reason: str = "",
        source: str = SOURCE_MANUAL,
        priority: int = 50,
        owner_user_id: str | None = None,
        due_at: datetime | None = None,
        cadence_enrollment_id: str | None = None,
        cadence_step_index: int | None = None,
    ) -> CallTask:
        """Queue a call. Idempotent: returns the existing OPEN task for the same contact (and the
        same cadence step, when cadence-driven) instead of creating a duplicate."""
        where = [CallTask.status == CALL_OPEN, CallTask.account_id == account_id]
        if contact_id is not None:
            where.append(CallTask.contact_id == contact_id)
        if cadence_enrollment_id is not None:
            where.append(CallTask.cadence_enrollment_id == cadence_enrollment_id)
            where.append(CallTask.cadence_step_index == cadence_step_index)
        existing = (await ts.session.scalars(ts.select(CallTask, *where).limit(1))).first()
        if existing is not None:
            return existing

        task = CallTask(
            tenant_id=ts.tenant_id,
            account_id=account_id,
            contact_id=contact_id,
            reason=reason,
            priority=max(0, min(int(priority), 100)),
            status=CALL_OPEN,
            source=source,
            owner_user_id=owner_user_id,
            due_at=due_at,
            cadence_enrollment_id=cadence_enrollment_id,
            cadence_step_index=cadence_step_index,
        )
        ts.add(task)
        await ts.flush()
        return task

    async def list_queue(
        self,
        ts: TenantSession,
        *,
        owner_user_id: str | None = None,
        status: str = CALL_OPEN,
        limit: int = 50,
    ) -> list[CallTask]:
        where = []
        if status in (CALL_OPEN, CALL_DONE, "skipped"):
            where.append(CallTask.status == status)
        if owner_user_id:
            where.append(CallTask.owner_user_id == owner_user_id)
        stmt = ts.select(CallTask, *where).order_by(CallTask.priority.desc()).limit(limit)
        return list((await ts.session.scalars(stmt)).all())

    async def generate_script(self, ts: TenantSession, task: CallTask) -> dict:
        """Run the call-script agent for the task's account/contact and cache it on the task."""
        from nexus.agents.runtime import get_agent_runtime

        result = await get_agent_runtime().run(
            "call_script", ts, account_id=task.account_id,
            contact_id=task.contact_id, persist=False,
        )
        script = (result.output or {}).get("script") or {}
        task.script_cache = script
        await ts.flush()
        return script

    async def log_disposition(
        self,
        ts: TenantSession,
        task_id: str,
        *,
        disposition: str,
        notes: str = "",
        duration_s: int | None = None,
        next_step: str | None = None,
    ) -> CallActivity | None:
        """Log a call outcome. Terminal dispositions close the task; re-queue dispositions
        (no_answer/callback/gatekeeper) keep it open so the SDR can try again."""
        task = await ts.get(CallTask, task_id)
        if task is None:
            return None
        activity = CallActivity(
            tenant_id=ts.tenant_id,
            call_task_id=task.id,
            account_id=task.account_id,
            contact_id=task.contact_id,
            disposition=disposition,
            notes=notes or "",
            duration_s=duration_s,
            next_step=next_step,
            occurred_at=utcnow(),
        )
        ts.add(activity)
        if disposition not in REQUEUE_DISPOSITIONS:
            task.status = CALL_DONE
        await ts.flush()
        return activity

    async def skip(self, ts: TenantSession, task_id: str) -> CallTask | None:
        task = await ts.get(CallTask, task_id)
        if task is None:
            return None
        task.status = "skipped"
        await ts.flush()
        return task

    async def list_activities(
        self, ts: TenantSession, *, contact_id: str, limit: int = 50
    ) -> list[CallActivity]:
        stmt = (
            ts.select(CallActivity, CallActivity.contact_id == contact_id)
            .order_by(CallActivity.occurred_at.desc())
            .limit(limit)
        )
        return list((await ts.session.scalars(stmt)).all())


_service = CallQueueService()


def get_call_queue_service() -> CallQueueService:
    return _service
