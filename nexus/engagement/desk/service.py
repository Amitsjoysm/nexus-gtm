"""The reply desk: what needs an answer, the answer the AI suggests, and the SDR's decision (§9, D22).

**Nothing here sends by itself.** The AI drafts; a person presses Send. That is D22, and it is why
`send_response` takes the text the SDR is looking at rather than re-drafting at the last moment.

Three tabs, one query each:

* **Needs action** — open classifications (`interested`, `question`, `referral`, `unclear`) plus the
  colleagues those replies paused, who never resume on their own (D3).
* **Scheduled** — enrollments waiting for a date: a re-engagement they asked for, or a return from
  leave. Both are editable and cancellable.
* **Handled** — declined, unsubscribed and bounced: the record of who may not be emailed and why.

A rep sees their own mailboxes; a manager sees the team's and can reassign.

Four decisions close an item, and each one is a different statement about the person:
`reengage` (come back on a date), `block` (do not contact), `close` (nothing more to do here),
`meeting` (which also writes an `Outcome`, so the dashboards and account tiering see it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

OPEN_CATEGORIES = ("interested", "question", "referral", "unclear")
HANDLED_CATEGORIES = ("declined", "unsubscribe")
DECISIONS = ("reengage", "block", "close", "meeting")
REENGAGE_AT = time(9, 0)
#: The working day the reply-speed promise is counted in (spec §19).
OFFICE_OPEN = time(9, 0)
OFFICE_CLOSE = time(18, 0)


class DeskError(ValueError):
    """A desk action the item's state does not allow."""


@dataclass(slots=True)
class DeskItem:
    classification: object
    message: object
    contact: object | None = None
    account: object | None = None
    enrollment: object | None = None
    conversation: list = field(default_factory=list)
    paused_colleagues: list = field(default_factory=list)


async def _mailbox_ids(ts, *, user_id: str, team: bool) -> list[str]:
    from nexus.models.engagement import MailboxConnection

    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    return [m.id for m in await ts.list(MailboxConnection, *where)]


async def needs_action(ts, *, user_id: str, team: bool = False) -> list:
    """Open classifications on the caller's mailboxes, oldest first — the queue, in order."""
    from nexus.models.engagement import ReplyClassification

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(ReplyClassification,
                         ReplyClassification.mailbox_connection_id.in_(mailboxes),
                         ReplyClassification.status == "open")
    return sorted(rows, key=lambda r: r.created_at)


async def scheduled(ts, *, user_id: str, team: bool = False) -> list:
    """Enrollments waiting for a date: a re-engagement, or a return from leave."""
    from sqlalchemy import or_

    from nexus.models.engagement import EngagementEnrollment

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(
        EngagementEnrollment,
        EngagementEnrollment.mailbox_connection_id.in_(mailboxes),
        or_(EngagementEnrollment.status == "snoozed",
            (EngagementEnrollment.status == "paused")
            & (EngagementEnrollment.status_reason == "out_of_office")))
    return sorted(rows, key=lambda r: r.snoozed_until or r.created_at)


async def handled(ts, *, user_id: str, team: bool = False, limit: int = 100) -> list:
    from nexus.models.engagement import ReplyClassification

    mailboxes = await _mailbox_ids(ts, user_id=user_id, team=team)
    if not mailboxes:
        return []
    rows = await ts.list(ReplyClassification,
                         ReplyClassification.mailbox_connection_id.in_(mailboxes),
                         ReplyClassification.status == "done")
    return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]


async def item(ts, classification) -> DeskItem:
    """One item with everything the screen shows: the conversation, who it is with, and the
    colleagues this reply paused."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    message = await ts.get(EngagementMessage, classification.message_id)
    conversation = []
    if message is not None and message.thread_id:
        conversation = list((await ts.session.scalars(
            ts.select(EngagementMessage, EngagementMessage.thread_id == message.thread_id,
                      EngagementMessage.status.in_(("sent", "received", "bounced")))
            .order_by(EngagementMessage.created_at.asc()))).all())
    colleagues = []
    if classification.account_id:
        colleagues = [e for e in await ts.list(
            EngagementEnrollment, EngagementEnrollment.account_id == classification.account_id,
            EngagementEnrollment.status == "paused",
            EngagementEnrollment.status_reason == "colleague_replied")]
    return DeskItem(
        classification=classification, message=message,
        contact=await ts.get(Contact, classification.contact_id)
        if classification.contact_id else None,
        account=await ts.get(Account, classification.account_id)
        if classification.account_id else None,
        enrollment=await ts.get(EngagementEnrollment, classification.enrollment_id)
        if classification.enrollment_id else None,
        conversation=conversation, paused_colleagues=colleagues)


async def draft_response(ts, classification, *, user_id: str) -> dict:
    """The AI's suggested reply, stored on the classification for the SDR to edit (D22)."""
    from nexus.engagement.drafting.drafter import REPLY_DRAFT, draft
    from nexus.engagement.ledger.emit import emit
    from nexus.models.account import Account
    from nexus.models.engagement import EngagementEnrollment, EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    if detail.contact is None or detail.message is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    account = detail.account or await ts.get(Account, detail.contact.account_id)
    thread = await ts.get(EngagementThread, detail.message.thread_id) \
        if detail.message.thread_id else None
    enrollment = detail.enrollment or EngagementEnrollment(
        campaign_id="", contact_id=detail.contact.id, account_id=getattr(account, "id", ""),
        contact_timezone=mailbox.timezone or "UTC", current_thread_id=getattr(thread, "id", None))
    written = await draft(ts, enrollment=enrollment, contact=detail.contact, account=account,
                          mailbox=mailbox, kind="response", thread=thread, user_id=user_id,
                          capability=REPLY_DRAFT)
    if not written.ok:
        raise DeskError(f"The response could not be drafted: {written.error}")
    classification.suggested_response = f"{written.subject}\n\n{written.body}".strip()
    await ts.flush()
    await emit(ts, "response.drafted", actor_user_id=user_id,
               refs={"message_id": detail.message.id, "contact_id": detail.contact.id,
                     "account_id": getattr(account, "id", None),
                     "classification_id": classification.id,
                     "thread_id": getattr(thread, "id", None), "mailbox_id": mailbox.id},
               payload={"subject": written.subject, "body": written.body,
                        "context_pack": written.context_pack, "category": classification.category})
    return {"subject": written.subject, "body": written.body,
            "quality_problems": written.problems}


async def send_response(ts, classification, *, user_id: str, subject: str, body: str):
    """Send the SDR's answer in the same thread. The text sent is the text they were looking at."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.sending.service import send
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    contact = detail.contact or (await ts.get(Contact, classification.contact_id)
                                 if classification.contact_id else None)
    if contact is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    thread = await ts.get(EngagementThread, detail.message.thread_id) \
        if detail.message is not None and detail.message.thread_id else None
    result = await send(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                        thread=thread, kind="response", user_id=user_id,
                        idempotency_key=f"response:{classification.id}",
                        ai_subject=(classification.suggested_response or "").split("\n", 1)[0],
                        ai_body=(classification.suggested_response or "").partition("\n\n")[2])
    if result.sent:
        classification.responded_at = utcnow()
        if classification.category != "unclear":
            classification.status = "done"
        await ts.flush()
        await emit(ts, "response.sent", actor_user_id=user_id,
                   refs={"message_id": result.message_id, "contact_id": contact.id,
                         "account_id": classification.account_id,
                         "classification_id": classification.id,
                         "thread_id": result.thread_id, "mailbox_id": mailbox.id,
                         "answered_message_id": classification.message_id},
                   payload={"subject": subject, "body": body,
                            "ai_body": (classification.suggested_response or "")
                            .partition("\n\n")[2],
                            "conversation": [{"direction": m.direction,
                                              "at": (m.sent_at or m.received_at or m.created_at)
                                              .isoformat(), "body": m.body_text}
                                             for m in detail.conversation]})
    return result


async def save_to_drafts(ts, classification, *, subject: str, body: str) -> str:
    """Put the answer in the SDR's own Drafts folder instead of sending it. Never metered: nothing
    left the building, and charging for pressing Save bills a customer for hesitating."""
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.sending.drafts import save_draft
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementThread, MailboxConnection

    detail = await item(ts, classification)
    contact = detail.contact or (await ts.get(Contact, classification.contact_id)
                                 if classification.contact_id else None)
    if contact is None:
        raise DeskError("This reply is not linked to a contact, so there is nobody to answer.")
    mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
    answered = detail.message
    thread = await ts.get(EngagementThread, answered.thread_id) \
        if answered is not None and answered.thread_id else None
    try:
        return await save_draft(ts, mailbox=mailbox, contact=contact, subject=subject, body=body,
                                answering=answered, thread=thread)
    except ProviderError as exc:
        raise DeskError(f"The draft could not be saved: {exc}") from exc


async def decide(ts, classification, decision: str, *, user_id: str,
                 reengage_on: date | None = None, note: str = "") -> str:
    """Close an item the way the SDR chose. Every decision is recorded in the ledger."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.engagement.timekeeping import at_local, zone_or_none
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment
    from nexus.outcomes.service import get_outcome_service

    if decision not in DECISIONS:
        raise DeskError(f"Unknown decision {decision!r}.")
    if decision == "reengage" and reengage_on is None:
        raise DeskError("Choose the date to come back on.")
    contact = await ts.get(Contact, classification.contact_id) \
        if classification.contact_id else None
    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.contact_id == classification.contact_id) \
        if classification.contact_id else []

    if decision == "reengage":
        for enrollment in enrollments:
            if enrollment.status == "completed":
                continue
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(reengage_on, REENGAGE_AT, zone)
            await set_status(ts, enrollment, "snoozed", "later", user_id=user_id)
    elif decision == "block":
        if contact is not None and contact.email:
            await suppress(ts, email=contact.email, reason="manual", contact_id=contact.id,
                           source_message_id=classification.message_id,
                           created_by_user_id=user_id)
        for enrollment in enrollments:
            if enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)
    elif decision == "meeting":
        from nexus.engagement.reports.outcomes import attribution

        enrollment = await ts.get(EngagementEnrollment, classification.enrollment_id)             if classification.enrollment_id else None
        await get_outcome_service().record(
            ts, stage="meeting", account_id=classification.account_id,
            contact_id=classification.contact_id,
            meta={**attribution(enrollment), "source": "engagement_reply_desk",
                  "message_id": classification.message_id,
                  "classification_id": classification.id})
        for enrollment in enrollments:
            if enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "replied", user_id=user_id)
    else:  # close
        for enrollment in enrollments:
            if enrollment.status == "paused" and enrollment.status_reason == "needs_decision":
                await set_status(ts, enrollment, "stopped", "manual", user_id=user_id)

    classification.decision = decision
    classification.decided_by_user_id = user_id
    classification.decided_at = utcnow()
    classification.status = "done"
    await ts.flush()
    await emit(ts, "reply.decided", actor_user_id=user_id,
               refs={"message_id": classification.message_id,
                     "contact_id": classification.contact_id,
                     "account_id": classification.account_id,
                     "classification_id": classification.id},
               payload={"decision": decision, "category": classification.category,
                        "reengage_on": reengage_on.isoformat() if reengage_on else "",
                        "note": note[:500]})
    return decision


async def correct(ts, classification, category: str, *, user_id: str) -> None:
    """The SDR says the AI read it wrong. The correction is the label the ledger trains on."""
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies.classify import CATEGORIES

    if category not in CATEGORIES:
        raise DeskError(f"Unknown category {category!r}.")
    previous = classification.category
    classification.corrected_category = category
    classification.label_source = "sdr_confirmed" if category == previous else "sdr_corrected"
    await ts.flush()
    await emit(ts, "reply.corrected", actor_user_id=user_id,
               refs={"message_id": classification.message_id,
                     "contact_id": classification.contact_id,
                     "account_id": classification.account_id,
                     "classification_id": classification.id},
               payload={"ai_category": previous, "sdr_category": category,
                        "resolved_date": classification.resolved_date.isoformat()
                        if classification.resolved_date else ""})


async def reassign(ts, classification, *, user_id: str) -> None:
    classification.assigned_user_id = user_id
    await ts.flush()


async def resume_colleague(ts, enrollment, *, user_id: str) -> None:
    from nexus.engagement.sequences.service import resume_enrollment

    if enrollment.status_reason != "colleague_replied":
        raise DeskError("This contact was not paused by a colleague's reply.")
    await resume_enrollment(ts, enrollment, user_id=user_id)


async def stop_colleague(ts, enrollment, *, user_id: str) -> None:
    from nexus.engagement.sequences.service import stop_enrollment

    await stop_enrollment(ts, enrollment, "manual", user_id=user_id)


def _is_scheduled(enrollment) -> bool:
    return enrollment.status == "snoozed" or (
        enrollment.status == "paused" and enrollment.status_reason == "out_of_office")


async def reschedule(ts, enrollment, when: datetime, *, user_id: str) -> None:
    """Move a scheduled contact's return date: a re-engagement they asked for, or the day they are
    back from leave. The worker wakes both on ``snoozed_until``, so that is the one field moved."""
    from nexus.core.db import utcnow
    from nexus.engagement.ledger.emit import emit

    if not _is_scheduled(enrollment):
        raise DeskError("Only a contact waiting for a date can be given a new one.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when <= utcnow():
        raise DeskError("Choose a date in the future, or resume the contact instead.")
    previous = enrollment.snoozed_until
    enrollment.snoozed_until = when
    await ts.flush()
    # A new date is a new snooze, so it is recorded as one: same event, same shape as
    # `set_status` writes, with the date it replaced.
    await emit(ts, "enrollment.snoozed", actor_user_id=user_id,
               refs={"enrollment_id": enrollment.id, "campaign_id": enrollment.campaign_id,
                     "contact_id": enrollment.contact_id, "account_id": enrollment.account_id},
               payload={"from": enrollment.status, "to": enrollment.status,
                        "reason": enrollment.status_reason or "",
                        "step_index": enrollment.current_step_index,
                        "until": when.isoformat(),
                        "rescheduled_from": previous.isoformat() if previous else ""})


async def cancel_scheduled(ts, enrollment, *, user_id: str) -> None:
    """The date is not wanted after all: stop the contact, so nothing more is sent."""
    from nexus.engagement.sequences.service import stop_enrollment

    if not _is_scheduled(enrollment):
        raise DeskError("Only a contact waiting for a date can be cancelled here.")
    await stop_enrollment(ts, enrollment, "manual", user_id=user_id)


async def remind_unanswered(ts, *, now: datetime) -> int:
    """Nudge the SDR when an interested buyer has been waiting (§19 reply-speed reminder).

    Business hours, not wall-clock hours: a reply that arrives on Friday evening is not late on
    Saturday morning. One reminder per reply — a second would be noise about the same fact.
    """
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.settings import read_settings
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection, ReplyClassification
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    hours = read_settings(getattr(tenant, "email_settings", None)).reply_reminder_business_hours
    waiting = await ts.list(ReplyClassification, ReplyClassification.status == "open",
                            ReplyClassification.category.in_(("interested", "question")),
                            ReplyClassification.responded_at.is_(None),
                            ReplyClassification.reminded_at.is_(None))
    reminded = 0
    for classification in waiting:
        mailbox = await ts.get(MailboxConnection, classification.mailbox_connection_id)
        # In the SDR's own working day: "four hours" means four hours they were at work.
        zone = zone_or_none(getattr(mailbox, "timezone", None)) or UTC
        if business_hours_between(classification.created_at, now, zone) < hours:
            continue
        contact = await ts.get(Contact, classification.contact_id) \
            if classification.contact_id else None
        await notify(ts, category=f"reply_{classification.category}",
                     owner_user_id=getattr(mailbox, "owner_user_id", None),
                     account_id=classification.account_id,
                     title=f"Still waiting for your reply to "
                           f"{getattr(contact, 'full_name', 'a buyer')}",
                     body=f"They wrote {hours}+ business hours ago and the answer is still a draft.",
                     meta={"classification_id": classification.id, "reminder": True})
        classification.reminded_at = now
        reminded += 1
    await ts.flush()
    return reminded


def business_hours_between(start: datetime, end: datetime, zone=UTC) -> float:
    """Working hours between two moments: Monday–Friday, 09:00–18:00 in `zone`. Pure.

    Wall-clock hours would make every reply that arrives at six in the evening late by breakfast,
    and the reminder would arrive while nobody is there to act on it. Counting whole weekdays has
    the same fault in a smaller way — it puts a four-hour promise at ten at night.
    """
    from datetime import timedelta

    from nexus.engagement.timekeeping import is_business_day

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if end <= start:
        return 0.0
    start, end = start.astimezone(zone), end.astimezone(zone)
    hours = 0.0
    day = start.date()
    while day <= end.date():
        if is_business_day(day):
            opens = datetime.combine(day, OFFICE_OPEN, tzinfo=zone)
            closes = datetime.combine(day, OFFICE_CLOSE, tzinfo=zone)
            overlap = min(end, closes) - max(start, opens)
            hours += max(0.0, overlap.total_seconds() / 3600)
        day += timedelta(days=1)
    return hours
