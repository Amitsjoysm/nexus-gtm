"""Call queue service: enqueue calls, generate AI scripts, log dispositions.

The queue is the SDR's prioritized call list; dispositions are logged as :class:`CallActivity`
rows (the call history + analytics source). Enqueue is idempotent (at most one OPEN task per
contact / per cadence step), mirroring the Inbox dedupe so an account/contact never piles up
duplicate calls. Cadence progression is decoupled: a cadence call-step queues a task and the
sequence advances on schedule (like an email send) — logging the outcome never blocks the cadence.
"""
from __future__ import annotations

import logging
from datetime import datetime

from nexus.calling.provider import (
    CallHandle,
    CallProviderError,
    TelephonyError,
    TelephonyNotConfigured,
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

logger = logging.getLogger("nexus.calling.service")


def _written_today(stamp: str | None, now: datetime) -> bool:
    """Whether an ISO timestamp falls on today's UTC date. Unparseable or missing means no."""
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(stamp).date() == now.date()
    except (TypeError, ValueError):
        return False


#: Priced at 4 credits a minute in `billing/rates.py` and, until 2026-09-23, charged by nothing.
MINUTES_CAPABILITY = "calling.minutes"


async def _charge_minutes(ts, provider_call_id: str, seconds, *, user_id: str | None) -> None:
    """Charge a platform-account call its started minutes, ONCE per call. Never raises.

    Keyed on the provider's call id, so logging a second outcome for the same call (a callback
    after a no-answer) cannot charge it twice. After the fact, like `routers/accounts._meter`: the
    call already happened, so a refusal here decides nothing — the preflight before the dial is
    what enforces. A call Twilio measured at 0 seconds was never answered and costs nothing.
    """
    import math

    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return
    from nexus.billing.meter import metered

    try:
        async with metered(
            ts, MINUTES_CAPABILITY, quantity=math.ceil(seconds / 60), user_id=user_id,
            source="calling", idempotency_key=f"call:{provider_call_id}",
            attrs={"provider_call_id": provider_call_id, "seconds": seconds},
        ):
            pass
    except Exception:
        logger.warning("could not charge minutes for call %s", provider_call_id, exc_info=True)

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

    async def generate_script(
        self, ts: TenantSession, task: CallTask, *, refresh: bool = False
    ) -> dict:
        """The call script for this task: today's cached one, or a freshly generated one.

        The cache was WRITTEN on every generation and READ by nothing, so the console regenerated —
        and paid for — a new script every time a call was opened. It is now reused, but only on the
        day it was written: a script is spoken live and can say "this week", and one from yesterday
        would read out days that have moved. A cache with no timestamp predates this and is treated
        as stale. ``refresh`` is the console's Regenerate button.
        """
        from nexus.agents.runtime import get_agent_runtime

        cached = task.script_cache or {}
        if not refresh and cached and _written_today(cached.get("generated_at"), utcnow()):
            return cached

        result = await get_agent_runtime().run(
            "call_script", ts, account_id=task.account_id,
            contact_id=task.contact_id, persist=False,
        )
        script = dict((result.output or {}).get("script") or {})
        if script:
            script["generated_at"] = utcnow().isoformat()
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
                    .order_by(SignalEvent.occurred_at.desc(), SignalEvent.created_at.desc())
                    .limit(25)
                )
            ).all()
        )
        # Only signals inside the platform window: the brief is read out on a call.
        from nexus.ingestion.window import within_ai_window

        ranked = sorted(
            within_ai_window(raw_signals), key=lambda s: (s.contact_id == cid, s.strength),
            reverse=True,
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
                # Lets the console say "found 3d ago" rather than dress an undated item as news.
                "dated": s.dated or "found",
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
        from nexus.models.account import Contact

        task = await ts.get(CallTask, task_id)
        if task is None:
            return None
        contact = await ts.get(Contact, task.contact_id) if task.contact_id else None
        phone = ((contact.phone if contact else "") or "").strip()
        if not phone:
            raise CallProviderError("This contact has no phone number to dial.")

        # The workspace's own Twilio when connected, else the platform's (calling/connection.py).
        from nexus.calling import connection

        resolved = await connection.resolve_call_provider(ts)
        provider, from_number = resolved.provider, resolved.from_number
        if provider.name != "stub" and not from_number:
            raise TelephonyNotConfigured(
                "Your workspace's Twilio connection has no caller ID. Add one under Integrations."
                if resolved.source == "workspace" else
                "No caller ID is set for calls. A superadmin sets it under Runtime settings "
                "(Calling caller ID), or NEXUS_TELEPHONY_FROM_NUMBER in the environment."
            )
        if resolved.source == "platform" and provider.name != "stub":
            # Minutes on OUR account cost credits. Asked BEFORE dialling: once the rep's phone
            # rings the minutes are spent, and a refusal then would only decide who pays for them.
            # One minute is the least any answered call costs; the measured total is charged at
            # disposition. A workspace on its own Twilio pays Twilio, so it is never asked.
            from nexus.billing.entitlements import preflight

            (await preflight(ts, MINUTES_CAPABILITY, quantity=1)).raise_if_blocked()
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
        user_id: str | None = None,
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
            from nexus.calling import connection

            try:
                resolved = await connection.resolve_call_provider(ts)
            except TelephonyError as exc:
                # Logging what happened on a call must never be blocked by the connection that
                # placed it having changed since. Nothing is fetched and nothing is charged.
                logger.warning("disposition for %s without provider lookup: %s", task_id, exc)
                resolved = None
            if resolved is not None:
                provider = resolved.provider
                status = await provider.get_call_status(provider_call_id)
                measured = status.get("duration_s") if status else None
                # The provider's duration is measured, not remembered — but an explicitly entered
                # value is the rep's deliberate correction, so it wins on the activity.
                if duration_s is None:
                    duration_s = measured
                recording_url = await provider.get_recording(provider_call_id)
                transcript = await provider.get_transcript(provider_call_id)
                if resolved.source == "platform":
                    # BILLED on the measured duration, not the typed one: the rep's number is a
                    # note, Twilio's is what we pay for. Typed only when Twilio could not say.
                    await _charge_minutes(
                        ts, provider_call_id, measured if measured is not None else duration_s,
                        user_id=user_id,
                    )

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
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "call.disposition",
                   refs={"account_id": task.account_id, "contact_id": task.contact_id,
                         "call_task_id": task.id, "call_activity_id": activity.id},
                   payload={"disposition": disposition, "notes": notes or "",
                            "duration_s": duration_s, "next_step": next_step,
                            "transcript": transcript or ""})
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
