"""Call queue service: enqueue calls, generate AI scripts, log dispositions.

The queue is the SDR's prioritized call list; dispositions are logged as :class:`CallActivity`
rows (the call history + analytics source). Enqueue is idempotent (at most one OPEN task per
contact / per cadence step), mirroring the Inbox dedupe so an account/contact never piles up
duplicate calls. Cadence progression is decoupled: a cadence call-step queues a task and the
sequence advances on schedule (like an email send) — logging the outcome never blocks the cadence.
"""
from __future__ import annotations

from datetime import datetime

from nexus.calling.provider import (
    CallHandle,
    CallProviderError,
    TelephonyNotConfigured,
    get_call_provider,
)
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

    async def build_brief(self, ts: TenantSession, task: CallTask) -> dict:
        """Assemble a pre-call research dossier for a call task: who the person is, their company,
        the buying signals (each with its own source), social insights, and grounded talking
        points — so the SDR walks in already researched. Read-only: it composes data that already
        exists (firmographics, signals, the ICP score's rationale, person personalization), and
        every block carries a source so the rep can trust and cite it on the call."""
        from nexus.models.account import Account, Contact
        from nexus.models.intelligence import AccountScore
        from nexus.models.signal import SignalEvent
        from nexus.personalization.brief import build_person_brief

        account = await ts.get(Account, task.account_id)
        contact = await ts.get(Contact, task.contact_id) if task.contact_id else None
        cid = contact.id if contact else None

        # Signals for the account (newest first), then ranked for THIS call: a signal tied to the
        # person leads, then by intrinsic strength. Each row carries its own source + url.
        raw_signals = list(
            (
                await ts.session.scalars(
                    ts.select(SignalEvent, SignalEvent.account_id == task.account_id)
                    .order_by(SignalEvent.occurred_at.desc())
                    .limit(25)
                )
            ).all()
        )
        ranked = sorted(
            raw_signals, key=lambda s: (s.contact_id == cid, s.strength), reverse=True
        )
        signals = [
            {
                "title": s.title,
                "body": s.body or "",
                "kind": s.kind,
                "source": s.source,
                "url": s.url,
                "strength": s.strength,
                "occurred_at": s.occurred_at.isoformat() if s.occurred_at else "",
                "is_personal": bool(cid and s.contact_id == cid),
            }
            for s in ranked[:6]
        ]

        # Latest ICP fit (the relevance engine's verdict + rationale is itself a citable source).
        score = (
            await ts.session.scalars(
                ts.select(AccountScore, AccountScore.account_id == task.account_id)
                .order_by(AccountScore.computed_at.desc())
                .limit(1)
            )
        ).first()

        account_block = None
        if account is not None:
            account_block = {
                "name": account.name,
                "domain": account.domain,
                "industry": account.industry,
                "employee_count": account.employee_count,
                "country": account.country,
                "tech_stack": list(account.tech_stack or []),
                "fit_score": score.composite if score else None,
                "fit_rationale": (score.rationale if score else "") or "",
                "source": _account_source_label(account),
            }

        person = build_person_brief(contact, account, raw_signals) if contact else None

        insights = None
        raw_ins = (contact.custom_fields or {}).get("personalization") if contact else None
        if isinstance(raw_ins, dict) and any(
            raw_ins.get(k) for k in ("headline", "summary", "recent_posts", "interests")
        ):
            insights = {
                "headline": raw_ins.get("headline", "") or "",
                "summary": raw_ins.get("summary", "") or "",
                "recent_posts": list(raw_ins.get("recent_posts") or []),
                "interests": list(raw_ins.get("interests") or []),
                "source": raw_ins.get("source", "") or "",
                "fetched_at": raw_ins.get("fetched_at"),
            }

        contact_block = None
        if contact is not None:
            contact_block = {
                "name": contact.full_name,
                "title": contact.title,
                "seniority": contact.seniority,
                "email": contact.email,
                "email_status": contact.email_status,
                "phone": contact.phone,
                "linkedin_url": contact.linkedin_url,
                "role_angle": person.role_angle if person else "",
                "source": contact.enrichment_source,
            }

        return {
            "contact": contact_block,
            "account": account_block,
            "insights": insights,
            "signals": signals,
            "talking_points": _talking_points(person, score, account),
        }

    async def place_call(
        self, ts: TenantSession, task_id: str, *, agent_number: str | None = None
    ) -> CallHandle | None:
        """Dial the task's contact through the configured telephony provider.

        Under the default stub this places no call and returns a click-to-dial ``tel:`` URL —
        the workflow reps use today. Under a live provider it rings ``agent_number`` (the rep's
        own phone) and bridges to the contact. Raises rather than degrading, so a failed dial is
        never reported as a placed call.
        """
        from nexus.core.config import get_settings
        from nexus.models.account import Contact

        task = await ts.get(CallTask, task_id)
        if task is None:
            return None
        contact = await ts.get(Contact, task.contact_id) if task.contact_id else None
        phone = ((contact.phone if contact else "") or "").strip()
        if not phone:
            raise CallProviderError("This contact has no phone number to dial.")

        provider = get_call_provider()
        from_number = (get_settings().telephony_from_number or "").strip()
        if provider.name != "stub" and not from_number:
            raise TelephonyNotConfigured(
                "NEXUS_TELEPHONY_FROM_NUMBER is not set. A live call needs a caller ID "
                "you own on the provider."
            )
        return await provider.place_call(
            to=phone,
            from_=from_number,
            context={
                "agent_number": agent_number,
                "task_id": task.id,
                "account_id": task.account_id,
                "contact_id": task.contact_id,
            },
        )

    async def log_disposition(
        self,
        ts: TenantSession,
        task_id: str,
        *,
        disposition: str,
        notes: str = "",
        duration_s: int | None = None,
        next_step: str | None = None,
        provider_call_id: str | None = None,
    ) -> CallActivity | None:
        """Log a call outcome. Terminal dispositions close the task; re-queue dispositions
        (no_answer/callback/gatekeeper) keep it open so the SDR can try again.

        When the call was placed live, ``provider_call_id`` pulls the provider's own record of
        it — real duration, recording URL, transcript — onto the activity. That lookup is
        best-effort: a provider hiccup, or a recording Twilio has not finished processing, must
        never stop the rep from logging what happened.
        """
        task = await ts.get(CallTask, task_id)
        if task is None:
            return None

        recording_url = transcript = None
        if provider_call_id:
            provider = get_call_provider()
            status = await provider.get_call_status(provider_call_id)
            # The provider's duration is measured, not remembered — but an explicitly entered
            # value is the rep's deliberate correction, so it wins.
            if duration_s is None and status:
                duration_s = status.get("duration_s")
            recording_url = await provider.get_recording(provider_call_id)
            transcript = await provider.get_transcript(provider_call_id)

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
            provider_call_id=provider_call_id,
            recording_url=recording_url,
            transcript=transcript,
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


def _account_source_label(account) -> str:
    """A human label for where the company's firmographics came from (the brief's provenance)."""
    if account.crm_source:
        return f"{account.crm_source.title()} (CRM)"
    src = (account.source or "").strip().lower()
    return {
        "auto_discovery": "ICP auto-discovery",
        "discovery": "Web discovery",
    }.get(src, src.replace("_", " ").title() if src else "Web enrichment")


def _talking_points(person, score, account) -> list[str]:
    """Grounded, citable openers for the call — built only from real data, never invented."""
    points: list[str] = []
    if person is not None and person.role_angle:
        points.append(f"Lead with what they care about: {person.role_angle}.")
    if person is not None and person.signal_title:
        whose = "they personally" if person.signal_is_personal else "their company"
        points.append(f"Open on what's timely — {whose}: {person.signal_title}.")
    if score is not None and score.composite:
        why = (score.rationale or "").strip()
        points.append(f"ICP fit {score.composite}/100" + (f" — {why}." if why else "."))
    if account is not None and account.tech_stack:
        points.append("Relevant tech in their stack: " + ", ".join(list(account.tech_stack)[:5]) + ".")
    return points


_service = CallQueueService()


def get_call_queue_service() -> CallQueueService:
    return _service
