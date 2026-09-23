"""The heartbeat's side of a campaign: find what is due, write it just in time, send it (§8, D5).

**Claiming.** Due enrollments are read across workspaces by id only, then each one is processed in
its own tenant session under ``SELECT ... FOR UPDATE SKIP LOCKED`` (spec §8), so two workers never
take the same enrollment and a slow one never blocks the rest. SQLite ignores the lock; the
integration suite proves it on Postgres. Even without the lock the send cannot happen twice — the
outbound row's partial unique index is the second line (phase 07).

**Just in time.** A follow-up is drafted when it is due, from the whole conversation as it stands
(D5): drafting at launch would write week-three emails before week one was answered, and spend
credits on people who replied.

**What each send outcome does to the enrollment:**

| send | enrollment |
|---|---|
| sent | next step scheduled from this send, or completed |
| held (mailbox, provider, retry) | stays due; looked at again after a short wait |
| held (quality) | the draft waits for review (D17) |
| held (credits) | paused `out_of_credits`, and so is the campaign (D18) |
| stopped | stopped, with the reason |
| failed | paused `needs_decision` |
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.sequences")

#: How long a held step waits before it is looked at again. Longer than a tick, so a paused
#: mailbox is not re-checked every minute; shorter than any delay a person would notice.
RETRY_AFTER = timedelta(minutes=15)
BATCH = 50


async def due_enrollments(now: datetime, limit: int = BATCH) -> list[tuple[str, str]]:
    """``[(tenant_id, enrollment_id)]`` due now, across workspaces, soonest first.

    Read on the platform sessionmaker: under the RLS-bound app role a cross-tenant read returns zero
    rows, and a heartbeat that finds nothing due looks exactly like a quiet day."""
    from sqlalchemy import or_, select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(EngagementEnrollment.tenant_id, EngagementEnrollment.id)
            .join(EngagementCampaign, EngagementCampaign.id == EngagementEnrollment.campaign_id)
            .where(EngagementCampaign.status == "active")
            .where(or_(
                (EngagementEnrollment.status == "active")
                & (EngagementEnrollment.next_action_at <= now),
                (EngagementEnrollment.status == "snoozed")
                & (EngagementEnrollment.snoozed_until <= now),
                (EngagementEnrollment.status == "paused")
                & (EngagementEnrollment.status_reason == "out_of_office")
                & (EngagementEnrollment.snoozed_until <= now),
            ))
            .order_by(EngagementEnrollment.next_action_at)
            .limit(limit)
        )).all()
    return [(tenant_id, enrollment_id) for tenant_id, enrollment_id in rows]


async def advance(now: datetime | None = None, limit: int = BATCH) -> dict:
    """One heartbeat's worth of sends. Never raises for one bad enrollment."""
    from nexus.core.db import utcnow
    from nexus.workers.tasks import tenant_session

    moment = now or utcnow()
    outcomes: dict[str, int] = {}
    for tenant_id, enrollment_id in await due_enrollments(moment, limit):
        try:
            async with tenant_session(tenant_id) as ts:
                outcome = await process(ts, enrollment_id, now=moment)
        except Exception:
            logger.warning("engagement enrollment %s failed to advance", enrollment_id,
                           exc_info=True)
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


async def process(ts, enrollment_id: str, *, now: datetime) -> str:
    """Advance one enrollment by at most one message. Returns what happened, in one word."""
    from nexus.engagement.sequences.service import set_status, steps_of
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementThread,
        MailboxConnection,
    )

    enrollment = (await ts.session.scalars(
        ts.select(EngagementEnrollment, EngagementEnrollment.id == enrollment_id)
        .with_for_update(skip_locked=True)
    )).first()
    if enrollment is None:
        return "locked"
    campaign = await ts.get(EngagementCampaign, enrollment.campaign_id)
    if campaign is None or campaign.status != "active":
        return "campaign_not_active"

    if (enrollment.status == "paused" and enrollment.status_reason == "out_of_office"
            and enrollment.snoozed_until and enrollment.snoozed_until <= now):
        # Back from out of office: resume the step it was on (§8). No extra email is added.
        enrollment.snoozed_until = None
        enrollment.next_action_at = now
        await set_status(ts, enrollment, "active")
    snoozed = enrollment.status == "snoozed" and enrollment.snoozed_until \
        and enrollment.snoozed_until <= now
    due = enrollment.status == "active" and enrollment.next_action_at \
        and enrollment.next_action_at <= now
    if not (snoozed or due):
        return "not_due"

    steps = await steps_of(ts, campaign)
    if not snoozed and enrollment.current_step_index >= len(steps):
        await set_status(ts, enrollment, "completed")
        return "completed"

    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    contact = await ts.get(Contact, enrollment.contact_id)
    account = await ts.get(Account, enrollment.account_id)
    thread = await ts.get(EngagementThread, enrollment.current_thread_id) \
        if enrollment.current_thread_id else None
    if mailbox is None or contact is None or account is None:
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "missing_records"
    if mailbox.status != "connected":
        await set_status(ts, enrollment, "paused", "mailbox_disconnected")
        return "mailbox_disconnected"

    if snoozed:
        return await _reengage(ts, campaign, enrollment, steps, mailbox, contact, account,
                               thread, now)

    step = steps[enrollment.current_step_index]
    if step.channel == "call":
        return await _call_step(ts, campaign, enrollment, steps, step, contact, now)
    return await _email_step(ts, campaign, enrollment, steps, step, mailbox, contact, account,
                             thread, now)


async def _email_step(ts, campaign, enrollment, steps, step, mailbox, contact, account, thread,
                      now) -> str:
    from nexus.engagement.drafting.drafter import draft
    from nexus.engagement.sequences.service import set_status
    from nexus.models.engagement import EngagementMessage

    index = enrollment.current_step_index
    row = await ts.first(EngagementMessage, EngagementMessage.enrollment_id == enrollment.id,
                         EngagementMessage.step_index == index,
                         EngagementMessage.direction == "out")
    context: dict = {}
    if row is not None and row.status in ("approved", "queued"):
        subject, body = row.subject, row.body_text
        ai_subject, ai_body, problems = row.ai_subject, row.ai_body, []
    elif row is not None and row.status == "draft":
        # Written, not yet approved: it waits for the SDR, it is not re-drafted.
        await set_status(ts, enrollment, "awaiting_review")
        return "awaiting_review"
    elif row is not None and row.status == "sent":
        return await _after_send(ts, campaign, enrollment, steps, row.thread_id, row.sent_at or now)
    elif row is not None and row.status == "failed":
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "failed"
    else:
        if index == 0:
            # A first email is reviewed before it goes (D4); an active enrollment with no approved
            # draft is a state the review queue must resolve, not one the worker papers over.
            await set_status(ts, enrollment, "awaiting_review")
            return "awaiting_review"
        written = await draft(ts, enrollment=enrollment, contact=contact, account=account,
                              mailbox=mailbox, step=step, kind="followup", thread=thread, now=now)
        if not written.ok:
            if written.error == "out_of_credits":
                return await _out_of_credits(ts, campaign, enrollment)
            enrollment.next_action_at = now + RETRY_AFTER
            await ts.flush()
            return "draft_failed"
        subject, body, problems = written.subject, written.body, written.problems
        ai_subject, ai_body = written.subject, written.body
        context = {"context_pack": written.context_pack,
                   "personalisation_facts": written.facts}
        if campaign.review_every_touch or problems:
            # Reviewed on request, or held because it failed a check nobody was there to see (D17).
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id,
                contact_id=contact.id, thread_id=getattr(thread, "id", None), direction="out",
                kind="step", status="draft", step_index=index, from_addr=mailbox.email,
                to_addrs=[contact.email], subject=subject, body_text=body,
                ai_subject=ai_subject, ai_body=ai_body, quality_problems=problems))
            await ts.flush()
            await set_status(ts, enrollment, "awaiting_review",
                             "needs_decision" if problems else None)
            return "awaiting_review"

    return await _send(ts, campaign, enrollment, steps, mailbox, contact, thread, subject, body,
                       ai_subject, ai_body, problems, "step", index, None, context, now)


async def _reengage(ts, campaign, enrollment, steps, mailbox, contact, account, thread,
                    now) -> str:
    """The one email a "later, with a date" reply earned (D6), in the same thread."""
    from nexus.engagement.drafting.drafter import draft

    written = await draft(ts, enrollment=enrollment, contact=contact, account=account,
                          mailbox=mailbox, kind="reengage", thread=thread, now=now)
    if not written.ok:
        if written.error == "out_of_credits":
            return await _out_of_credits(ts, campaign, enrollment)
        enrollment.snoozed_until = now + RETRY_AFTER
        await ts.flush()
        return "draft_failed"
    key = f"reengage:{enrollment.id}:{enrollment.snoozed_until.isoformat()}"
    return await _send(ts, campaign, enrollment, steps, mailbox, contact, thread, written.subject,
                       written.body, written.subject, written.body, written.problems, "reengage",
                       None, key, {"context_pack": written.context_pack,
                                   "personalisation_facts": written.facts}, now)


async def _send(ts, campaign, enrollment, steps, mailbox, contact, thread, subject, body,
                ai_subject, ai_body, problems, kind, step_index, key, context, now) -> str:
    from nexus.engagement.sending.service import send
    from nexus.engagement.sequences.service import set_status

    result = await send(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                        enrollment=enrollment, thread=thread, step_index=step_index, kind=kind,
                        ai_subject=ai_subject, ai_body=ai_body, quality_problems=problems,
                        idempotency_key=key, user_id=campaign.owner_user_id, context=context)
    if result.outcome == "sent":
        if kind == "reengage":
            enrollment.snoozed_until = None
            await set_status(ts, enrollment, "active")
        return await _after_send(ts, campaign, enrollment, steps, result.thread_id, now,
                                 advance_step=kind == "step")
    if result.outcome == "stopped":
        reason = _stop_reason(result.reason)
        await set_status(ts, enrollment, "stopped", reason)
        return f"stopped:{reason}"
    if result.outcome == "failed":
        await set_status(ts, enrollment, "paused", "needs_decision")
        return "failed"
    if "cover" in result.reason:
        return await _out_of_credits(ts, campaign, enrollment)
    wait = max(now + RETRY_AFTER, mailbox.paused_until or now)
    if kind == "reengage":
        enrollment.snoozed_until = wait
    else:
        enrollment.next_action_at = wait
    await ts.flush()
    return "held"


async def _after_send(ts, campaign, enrollment, steps, thread_id, sent_at, *,
                      advance_step: bool = True) -> str:
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.sequences.timing import followup_due_at, zone_name_for
    from nexus.models.engagement import MailboxConnection

    if thread_id:
        enrollment.current_thread_id = thread_id
    if advance_step:
        enrollment.current_step_index += 1
    enrollment.next_action_override = False
    if enrollment.current_step_index >= len(steps):
        await set_status(ts, enrollment, "completed")
        return "sent_completed"
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    enrollment.next_action_at = followup_due_at(
        step=steps[enrollment.current_step_index], previous_sent_at=sent_at,
        zone_name=zone_name_for(enrollment, campaign, mailbox))
    await ts.flush()
    return "sent"


async def _call_step(ts, campaign, enrollment, steps, step, contact, now) -> str:
    """A call step becomes a task in the rep's call queue, and the sequence moves on."""
    from nexus.models.calling import CallTask

    ts.add(CallTask(account_id=enrollment.account_id, contact_id=contact.id,
                    reason=step.angle or f"Step {step.step_index + 1} of {campaign.name}",
                    priority=60, owner_user_id=campaign.owner_user_id, due_at=now,
                    engagement_enrollment_id=enrollment.id))
    await ts.flush()
    return await _after_send(ts, campaign, enrollment, steps, enrollment.current_thread_id, now)


async def _out_of_credits(ts, campaign, enrollment) -> str:
    """D18: a campaign that runs out mid-way pauses — every contact, not only this one."""
    from nexus.engagement.sequences.service import set_status

    await set_status(ts, enrollment, "paused", "out_of_credits")
    campaign.status = "paused"
    campaign.pause_reason = "out_of_credits"
    await ts.flush()
    return "out_of_credits"


def _stop_reason(reason: str) -> str:
    text = (reason or "").lower()
    for word in ("unsubscribed", "declined", "bounced"):
        if word in text:
            return word
    if "replied" in text:
        return "replied"
    return "manual"
