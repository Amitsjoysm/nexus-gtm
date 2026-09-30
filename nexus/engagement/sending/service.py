"""Sending one email from an SDR's own mailbox, exactly once (spec §5).

The shape of this module is set by one requirement: **a send that we are not sure about must never
become a second email to a real buyer.** So:

1. The outbound row is written **before** the provider call, carrying the `Message-ID` and the
   `X-Nexus-Ref` the message will have. The partial unique index on
   ``(enrollment_id, step_index) WHERE direction = 'out'`` means a concurrent worker writing the
   same step loses to a constraint rather than to a race.
2. A retry **reuses that row**, with the same ids, and asks the provider's Sent folder for the ref
   before sending anything (``find_sent``). A timeout is therefore recoverable without a duplicate.
3. A provider quota or rate limit pauses the MAILBOX until its reset time and leaves the row queued.
   The step stays due; nothing is lost and nothing is sent twice.

The pre-send checks in `checks.py` run here, immediately before the provider call, because the
person can unsubscribe or reply between the scheduling and the send.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

logger = logging.getLogger("nexus.engagement.sending")

#: A provider that rate-limits without saying for how long. Long enough to matter, short enough that
#: a mailbox is not out of action for an afternoon.
DEFAULT_PAUSE_MINUTES = 30


@dataclass(slots=True)
class SendResult:
    outcome: str                 # sent | held | stopped | failed
    message_id: str = ""         # our row id
    provider_message_id: str = ""
    thread_id: str = ""
    reason: str = ""
    reconciled: bool = False     # the provider had it already; we did not send again

    @property
    def sent(self) -> bool:
        return self.outcome == "sent"


async def send(
    ts,
    *,
    mailbox,
    contact,
    subject: str,
    body: str,
    enrollment=None,
    thread=None,
    step_index: int | None = None,
    kind: str = "step",
    ai_subject: str | None = None,
    ai_body: str | None = None,
    quality_problems: list[str] | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    context: dict | None = None,
    inbound_since: datetime | None = None,
) -> SendResult:
    """Send one message, or say why it was held or stopped. Never sends the same message twice."""
    from nexus.billing.entitlements import preflight
    from nexus.billing.meter import metered
    from nexus.core.db import utcnow
    from nexus.engagement.mailboxes.provider import (
        AuthExpired,
        NotFound,
        ProviderError,
        ProviderLimit,
        ThreadRef,
        TransientError,
    )
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.sending import checks, mime
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import unsubscribe_url

    now = utcnow()
    to_addr = (getattr(contact, "email", "") or "").strip()
    if not to_addr:
        return SendResult("stopped", reason="the contact has no email address")

    block = await active_block(ts, to_addr)
    decision = checks.decide(
        suppressed_reason=getattr(block, "reason", "") if block else "",
        inbound_since_scheduled=bool(inbound_since),
        enrollment_status=getattr(enrollment, "status", "active"),
        mailbox_status=mailbox.status,
        mailbox_paused_until=mailbox.paused_until,
        quality_problems=quality_problems,
        is_first_touch=(step_index == 0),
        credits_ok=(await preflight(ts, "outreach.email_send")).allowed,
        now=now,
    )
    if not decision.ok:
        return SendResult("stopped" if decision.outcome == checks.STOP else "held",
                          reason=decision.reason)

    row, existing = await _claim_row(
        ts, mailbox=mailbox, contact=contact, enrollment=enrollment, thread=thread,
        step_index=step_index, kind=kind, subject=subject, body=body, to_addr=to_addr,
        ai_subject=ai_subject, ai_body=ai_body, quality_problems=quality_problems,
        idempotency_key=idempotency_key,
    )
    if row.status == "sent":
        return SendResult("sent", message_id=row.id,
                          provider_message_id=row.provider_message_id or "",
                          thread_id=row.thread_id or "", reconciled=True)
    if row.status == "failed":
        return SendResult("failed", message_id=row.id, reason=row.error or "")

    # Opening the provider refreshes the access token, which is itself a call to Google or
    # Microsoft and fails in the same three ways a send does. Outside this handling, an expired
    # grant raised out of the worker instead of holding the message and asking for a reconnect.
    try:
        provider = await open_provider(ts, mailbox)
    except AuthExpired:
        return SendResult("held", message_id=row.id, reason="the mailbox needs reconnecting")
    except (TransientError, TimeoutError):
        return SendResult("held", message_id=row.id,
                          reason="the provider did not answer; the send will be retried")
    except ProviderError as exc:
        # `fresh_access_token` has already marked the mailbox `error` with the provider's words.
        return SendResult("held", message_id=row.id, reason=f"the mailbox cannot sign in: {exc}")

    if existing:
        # This row has been through a provider call before. Ask the Sent folder first: a timeout is
        # the one failure where "try again" and "already delivered" look identical from here.
        found = await _find_already_sent(provider, row, to_addr, now)
        if found is not None:
            await _mark_sent(ts, row, found, now)
            return SendResult("sent", message_id=row.id,
                              provider_message_id=found.provider_message_id,
                              thread_id=row.thread_id or "", reconciled=True)

    thread_ref = None
    if thread is not None:
        parent = await _latest_in_thread(ts, thread, exclude_id=row.id)
        thread_ref = ThreadRef(
            provider_thread_id=thread.provider_thread_id,
            reply_to_provider_message_id=getattr(parent, "provider_message_id", "") or "",
            in_reply_to=row.in_reply_to or "",
            references=row.references_header or "",
        )

    message = mime.build_message(
        from_addr=mailbox.email, from_name=mailbox.display_name, to_addr=to_addr,
        subject=subject, body=body, message_id=row.rfc_message_id, ref=row.ref_header,
        unsubscribe_url=unsubscribe_url(ts.tenant_id, contact.id),
        unsubscribe_mailto=mailbox.email,
        signature=await _signature(ts, mailbox), in_reply_to=row.in_reply_to or "",
        references=row.references_header or "",
    )

    try:
        async with metered(ts, "outreach.email_send", user_id=user_id, source="engagement",
                           idempotency_key=f"outreach.email_send:{row.id}"):
            sent = await provider.send(mime.to_bytes(message), thread=thread_ref)
    except ProviderLimit as exc:
        await _pause_mailbox(ts, mailbox, exc.retry_at, now)
        row.error = f"provider limit: {exc}"[:500]
        await ts.flush()
        return SendResult("held", message_id=row.id,
                          reason="the mailbox hit its provider sending limit and is paused")
    except AuthExpired:
        mailbox.status = "needs_reauth"
        row.error = "the mailbox grant expired"
        await ts.flush()
        return SendResult("held", message_id=row.id, reason="the mailbox needs reconnecting")
    except (TransientError, TimeoutError) as exc:
        found = await _find_already_sent(provider, row, to_addr, now)
        if found is not None:
            await _mark_sent(ts, row, found, now)
            await _record(ts, row, mailbox=mailbox, contact=contact, enrollment=enrollment,
                          user_id=user_id, context=context or {}, now=now)
            return SendResult("sent", message_id=row.id,
                              provider_message_id=found.provider_message_id,
                              thread_id=row.thread_id or "", reconciled=True)
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        await ts.flush()
        return SendResult("held", message_id=row.id,
                          reason="the provider did not answer; the send will be retried")
    except (NotFound, ProviderError) as exc:
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        await ts.flush()
        return SendResult("failed", message_id=row.id, reason=str(exc)[:200])

    await _mark_sent(ts, row, sent, now)
    await _record(ts, row, mailbox=mailbox, contact=contact, enrollment=enrollment,
                  user_id=user_id, context=context or {}, now=now)
    return SendResult("sent", message_id=row.id,
                      provider_message_id=row.provider_message_id or "",
                      thread_id=row.thread_id or "")


async def _claim_row(ts, *, mailbox, contact, enrollment, thread, step_index, kind, subject, body,
                     to_addr, ai_subject, ai_body, quality_problems, idempotency_key):
    """The outbound row, created or re-found. Returns ``(row, existed_before)``.

    The unique index is the arbiter, not a read-then-write: two workers claiming the same step at
    the same moment is exactly the case a SELECT-then-INSERT would send twice."""
    from sqlalchemy.exc import IntegrityError

    from nexus.engagement.ids import new_ulid
    from nexus.engagement.sending import mime
    from nexus.models.engagement import EngagementMessage

    existing = await _existing(ts, enrollment, step_index, idempotency_key)
    if existing is not None and existing.status in ("draft", "approved"):
        # The draft the SDR approved (phase 08) IS the outbound row: adopt it, so the text that
        # was approved is exactly the text that is sent, and give it the ids it will carry.
        parent = await _latest_in_thread(ts, thread, exclude_id=existing.id)
        existing.thread_id = existing.thread_id or getattr(thread, "id", None)
        existing.subject, existing.body_text = subject, body
        existing.rfc_message_id = existing.rfc_message_id or mime.new_rfc_message_id(
            _domain(mailbox.email))
        existing.ref_header = existing.ref_header or new_ulid()
        existing.in_reply_to = getattr(parent, "rfc_message_id", "") or ""
        existing.references_header = mime.references_for(
            getattr(parent, "references_header", "") or "",
            getattr(parent, "rfc_message_id", "") or "")
        existing.status = "queued"
        await ts.flush()
        return existing, False
    if existing is not None:
        return existing, True

    parent = await _latest_in_thread(ts, thread)
    row = EngagementMessage(
        mailbox_connection_id=mailbox.id,
        thread_id=getattr(thread, "id", None),
        enrollment_id=getattr(enrollment, "id", None),
        contact_id=contact.id,
        direction="out",
        kind=kind,
        status="queued",
        step_index=step_index,
        idempotency_key=idempotency_key,
        rfc_message_id=mime.new_rfc_message_id(_domain(mailbox.email)),
        ref_header=new_ulid(),
        in_reply_to=getattr(parent, "rfc_message_id", "") or "",
        references_header=mime.references_for(
            getattr(parent, "references_header", "") or "",
            getattr(parent, "rfc_message_id", "") or ""),
        from_addr=mailbox.email,
        to_addrs=[to_addr],
        subject=subject,
        body_text=body,
        ai_subject=ai_subject,
        ai_body=ai_body,
        quality_problems=list(quality_problems or []),
    )
    try:
        async with ts.session.begin_nested():
            ts.add(row)
            await ts.flush()
    except IntegrityError:
        # Another worker claimed this step between our read and our write. Theirs is the row.
        other = await _existing(ts, enrollment, step_index, idempotency_key)
        if other is None:
            raise
        return other, True
    return row, False


async def _existing(ts, enrollment, step_index, idempotency_key):
    from nexus.models.engagement import EngagementMessage

    if enrollment is not None and step_index is not None:
        stmt = (ts.select(EngagementMessage)
                .where(EngagementMessage.enrollment_id == enrollment.id)
                .where(EngagementMessage.step_index == step_index)
                .where(EngagementMessage.direction == "out"))
        found = (await ts.session.scalars(stmt)).first()
        if found is not None:
            return found
    if idempotency_key:
        stmt = (ts.select(EngagementMessage)
                .where(EngagementMessage.idempotency_key == idempotency_key))
        return (await ts.session.scalars(stmt)).first()
    return None


async def _latest_in_thread(ts, thread, *, exclude_id: str | None = None):
    """The message a reply should answer: the newest in the thread, inbound or outbound (D16)."""
    from nexus.models.engagement import EngagementMessage

    if thread is None:
        return None
    stmt = (ts.select(EngagementMessage)
            .where(EngagementMessage.thread_id == thread.id)
            .where(EngagementMessage.status != "queued")
            .order_by(EngagementMessage.created_at.desc()))
    if exclude_id:
        stmt = stmt.where(EngagementMessage.id != exclude_id)
    return (await ts.session.scalars(stmt.limit(1))).first()


async def _find_already_sent(provider, row, to_addr: str, now: datetime):
    """Ask the Sent folder whether this exact message is already there. Never raises."""
    try:
        return await provider.find_sent(ref_header=row.ref_header, to=to_addr,
                                        around=row.created_at or now)
    except Exception:
        logger.warning("could not reconcile message %s against the Sent folder", row.id,
                       exc_info=True)
        return None


async def _mark_sent(ts, row, sent, now: datetime) -> None:
    row.status = "sent"
    row.sent_at = now
    row.error = None
    row.provider_message_id = sent.provider_message_id or row.provider_message_id
    if sent.provider_thread_id:
        thread = await thread_for(ts, row, sent.provider_thread_id, now)
        row.thread_id = thread.id
    await ts.flush()


async def thread_for(ts, row, provider_thread_id: str, now: datetime):
    """The thread row for this provider conversation, created on first sight.

    Unique on ``(mailbox_connection_id, provider_thread_id)``, so two messages landing in one
    conversation converge on one row instead of splitting the timeline."""
    from sqlalchemy.exc import IntegrityError

    from nexus.engagement.subjects import normalize_subject
    from nexus.models.engagement import EngagementThread

    stmt = (ts.select(EngagementThread)
            .where(EngagementThread.mailbox_connection_id == row.mailbox_connection_id)
            .where(EngagementThread.provider_thread_id == provider_thread_id))
    thread = (await ts.session.scalars(stmt)).first()
    if thread is not None:
        thread.last_message_at = now
        await ts.flush()
        return thread
    thread = EngagementThread(
        mailbox_connection_id=row.mailbox_connection_id,
        provider_thread_id=provider_thread_id,
        contact_id=row.contact_id,
        enrollment_id=row.enrollment_id,
        base_subject=normalize_subject(row.subject),
        last_message_at=now,
    )
    try:
        async with ts.session.begin_nested():
            ts.add(thread)
            await ts.flush()
    except IntegrityError:
        thread = (await ts.session.scalars(stmt)).first()
        if thread is None:
            raise
    return thread


async def _pause_mailbox(ts, mailbox, retry_at: datetime | None, now: datetime) -> None:
    """A provider limit pauses the MAILBOX, not the campaign: the limit is per sending account, and
    the steps stay due so they resume the moment it lifts (D10)."""
    mailbox.paused_until = retry_at or (now + timedelta(minutes=DEFAULT_PAUSE_MINUTES))
    mailbox.last_error = "paused until the provider sending limit resets"
    await ts.flush()


async def _signature(ts, mailbox) -> str:
    """The mailbox's own sign-off, else the workspace default (`outreach/signature.py`).

    One primary-key read per send. The alternative — passing it in — would make every caller
    responsible for a rule that belongs to the mailbox, and the rule already exists."""
    from nexus.models.identity import Tenant
    from nexus.outreach.signature import resolve_signature

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return resolve_signature(getattr(tenant, "email_settings", None),
                             {"signature": mailbox.signature})


async def _record(ts, row, *, mailbox, contact, enrollment, user_id, context: dict,
                  now: datetime) -> None:
    """The ledger event for a message that left the building (spec §18.4 `message.sent`)."""
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.timekeeping import to_local, zone_or_none

    zone_name = getattr(enrollment, "contact_timezone", "") or mailbox.timezone or "UTC"
    local = to_local(now, zone_or_none(zone_name) or UTC)
    payload = {
        "kind": row.kind,
        "step_index": row.step_index,
        "subject": row.subject,
        "body": row.body_text,
        "sent_local_hour": local.hour,
        "sent_local_weekday": local.weekday(),
        "contact_timezone": zone_name,
        "mailbox_provider": mailbox.provider,
    }
    payload.update(context or {})
    from nexus.engagement.reports.outcomes import record_sent

    # The dashboards, the ROI rollup and account tiering read `outcomes` (spec §11).
    await record_sent(ts, row, enrollment)
    await emit(ts, "message.sent", actor_user_id=user_id,
               refs={"contact_id": contact.id, "account_id": getattr(contact, "account_id", None),
                     "enrollment_id": getattr(enrollment, "id", None),
                     "campaign_id": getattr(enrollment, "campaign_id", None),
                     "thread_id": row.thread_id, "message_id": row.id, "mailbox_id": mailbox.id},
               payload=payload)


def _domain(email: str) -> str:
    return email.split("@", 1)[1] if "@" in (email or "") else ""
