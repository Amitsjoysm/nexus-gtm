"""Pull what arrived in a mailbox and turn each message into a stored, classified, acted-on reply (§6).

Notifications (Gmail Pub/Sub, Graph subscriptions) only say "something changed"; this module does
the reading, from the stored cursor, and the heartbeat runs it every few minutes as the fallback for
a notification that never came. Running it twice is harmless: a message is unique per mailbox by its
provider id, so the second copy is skipped before anything is written.

Per message, in order:

1. Skip our own sends.
2. A bounce → the message it reports is marked `bounced`, the address goes on do-not-contact
   (`bounced`), that enrollment stops. The bounce itself is not stored: it is about our email.
3. Match it (matching.py). **No match → nothing is stored** (privacy filter).
4. Store it in its conversation — moving the enrollment's current thread there if the person wrote
   in a different one (D16) — and record `reply.received`.
5. Read it (classify.py), apply the rules, act (actions.py), record `reply.classified`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("nexus.engagement.replies")

#: After a cursor the provider has forgotten, how far back to look.
RESYNC_WINDOW = timedelta(days=3)


async def sync_mailbox(ts, mailbox, *, now: datetime | None = None) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.mailboxes.provider import AuthExpired, CursorExpired, ProviderLimit
    from nexus.engagement.mailboxes.registry import open_provider

    moment = now or utcnow()
    if mailbox.status != "connected":
        return {"skipped": mailbox.status}
    try:
        provider = await open_provider(ts, mailbox)
        try:
            batch = await provider.fetch_changes(mailbox.sync_cursor)
        except CursorExpired:
            since = (mailbox.last_synced_at or moment) - RESYNC_WINDOW
            batch = await provider.resync(since)
    except AuthExpired:
        mailbox.status = "needs_reauth"
        await ts.flush()
        from nexus.engagement.replies.alerts import notify

        await notify(ts, category="mailbox_needs_reauth", owner_user_id=mailbox.owner_user_id,
                     title=f"Reconnect {mailbox.email}",
                     body="Replies cannot be read until the mailbox is reconnected.")
        return {"error": "needs_reauth"}
    except ProviderLimit as exc:
        return {"error": f"provider limit until {exc.retry_at}"}

    outcomes: dict[str, int] = {}
    for provider_message_id in batch.message_ids:
        try:
            async with ts.session.begin_nested():
                outcome = await ingest_message(ts, mailbox, provider, provider_message_id,
                                               now=moment)
        except Exception:
            logger.warning("could not ingest message %s for mailbox %s", provider_message_id,
                           mailbox.id, exc_info=True)
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    mailbox.sync_cursor = batch.next_cursor or mailbox.sync_cursor
    mailbox.last_synced_at = moment
    await ts.flush()
    return outcomes


async def ingest_message(ts, mailbox, provider, provider_message_id: str, *,
                         now: datetime) -> str:
    from nexus.engagement.replies import matching
    from nexus.engagement.replies.parse import parse
    from nexus.models.engagement import EngagementMessage

    if await ts.first(EngagementMessage,
                      EngagementMessage.mailbox_connection_id == mailbox.id,
                      EngagementMessage.provider_message_id == provider_message_id) is not None:
        return "duplicate"
    inbound = await provider.get_message(provider_message_id)
    if inbound.outgoing:
        return "own_send"
    parsed = parse(inbound.raw)
    if parsed.from_addr and parsed.from_addr == (mailbox.email or "").lower():
        return "own_send"
    if parsed.is_bounce:
        return await _bounce(ts, mailbox, parsed)

    found = await matching.match(ts, mailbox=mailbox, parsed=parsed,
                                 provider_thread_id=inbound.provider_thread_id)
    if not found.stored:
        return "discarded"

    thread = await _thread(ts, mailbox, found, inbound, parsed, now)
    row = EngagementMessage(
        mailbox_connection_id=mailbox.id, thread_id=thread.id, contact_id=getattr(
            found.contact, "id", None),
        enrollment_id=getattr(found.enrollments[0], "id", None) if found.enrollments else None,
        direction="in", kind="reply", status="received",
        provider_message_id=inbound.provider_message_id, rfc_message_id=parsed.message_id or None,
        in_reply_to=parsed.in_reply_to or None, references_header=" ".join(parsed.references),
        from_addr=parsed.from_addr, to_addrs=parsed.to_addrs, cc_addrs=parsed.cc_addrs,
        subject=parsed.subject, body_text=parsed.full_text,
        inbound_kind="auto_reply" if parsed.is_auto else "human",
        received_at=inbound.received_at or now)
    ts.add(row)
    await ts.flush()
    for enrollment in found.enrollments:
        # A reply in another thread moves the conversation there (D16).
        enrollment.current_thread_id = thread.id
    await _record_received(ts, row, found, mailbox, parsed)
    return await _classify_and_act(ts, row, found, mailbox, parsed)


async def _thread(ts, mailbox, found, inbound, parsed, now):
    from nexus.engagement.sending.service import thread_for

    if found.thread is not None and (not inbound.provider_thread_id or
                                     found.thread.provider_thread_id == inbound.provider_thread_id):
        found.thread.last_message_at = now
        await ts.flush()
        return found.thread

    class _Seed:  # thread_for reads these off the message it is given
        mailbox_connection_id = mailbox.id
        contact_id = getattr(found.contact, "id", None)
        enrollment_id = getattr(found.enrollments[0], "id", None) if found.enrollments else None
        subject = parsed.subject

    thread = await thread_for(ts, _Seed, inbound.provider_thread_id or parsed.message_id, now)
    if found.account is not None and not thread.account_id:
        thread.account_id = found.account.id
    await ts.flush()
    return thread


async def _classify_and_act(ts, row, found, mailbox, parsed) -> str:
    from nexus.engagement.drafting.context import conversation_block
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies import actions, classify
    from nexus.engagement.settings import effective_confidence, read_settings
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.engagement import EngagementMessage, ReplyClassification
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    settings = read_settings(getattr(tenant, "email_settings", None))
    threshold = effective_confidence(settings, mailbox.reply_confidence)
    zone_name = (found.enrollments[0].contact_timezone if found.enrollments
                 else mailbox.timezone) or "UTC"
    history = await ts.session.scalars(
        ts.select(EngagementMessage, EngagementMessage.thread_id == row.thread_id,
                  EngagementMessage.id != row.id,
                  EngagementMessage.status.in_(("sent", "received")))
        .order_by(EngagementMessage.created_at.asc()))
    context_text = conversation_block(list(history.all()))

    if found.rule == "colleague":
        reading = classify.Reading("referral", 1.0, "", "a colleague of the contact replied",
                                   "deterministic")
    else:
        reading = await classify.read(ts, parsed=parsed, context_text=context_text,
                                      user_id=mailbox.owner_user_id)
    verdict = classify.finalize(reading, text=parsed.text, threshold=threshold,
                                received_at=row.received_at, zone=zone_or_none(zone_name) or UTC,
                                ooo_default_days=settings.ooo_default_days)
    classification = ReplyClassification(
        message_id=row.id, mailbox_connection_id=mailbox.id,
        enrollment_id=row.enrollment_id, contact_id=row.contact_id,
        account_id=getattr(found.account, "id", None), category=verdict.category,
        confidence=verdict.confidence, date_phrase=verdict.date_phrase or None,
        resolved_date=verdict.resolved_date, reasoning=verdict.reasoning,
        label_source=verdict.label_source, assigned_user_id=mailbox.owner_user_id,
        status="open" if verdict.category in ("interested", "question", "referral", "unclear")
        else "done")
    ts.add(classification)
    await ts.flush()
    if found.rule == "colleague":
        action = await actions.apply_colleague(ts, match=found, inbound=row, mailbox=mailbox,
                                               classification=classification)
    else:
        action = await actions.apply(ts, verdict=verdict, match=found, inbound=row,
                                     mailbox=mailbox, classification=classification)
    classification.action_taken = action[:40]
    await ts.flush()
    from nexus.engagement.reports.outcomes import record_replied

    await record_replied(ts, row, classification)
    await emit(ts, "reply.classified", refs=_refs(row, found, mailbox, classification),
               payload={"category": verdict.category, "confidence": verdict.confidence,
                        "date_phrase": verdict.date_phrase,
                        "resolved_date": verdict.resolved_date.isoformat()
                        if verdict.resolved_date else "",
                        "label_source": verdict.label_source, "body": parsed.text,
                        "action": action})
    return f"classified:{verdict.category}"


async def _record_received(ts, row, found, mailbox, parsed) -> None:
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.timekeeping import to_local, zone_or_none

    zone_name = (found.enrollments[0].contact_timezone if found.enrollments
                 else mailbox.timezone) or "UTC"
    local = to_local(row.received_at, zone_or_none(zone_name) or UTC)
    latency = None
    if found.answered is not None and found.answered.sent_at:
        sent_at = found.answered.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        latency = int((row.received_at - sent_at).total_seconds())
    await emit(ts, "reply.received", refs=_refs(row, found, mailbox, None),
               payload={"inbound_kind": row.inbound_kind, "local_hour": local.hour,
                        "local_weekday": local.weekday(), "response_latency_s": latency,
                        "body": parsed.text, "match_rule": found.rule})


def _refs(row, found, mailbox, classification) -> dict:
    return {"message_id": row.id, "thread_id": row.thread_id, "mailbox_id": mailbox.id,
            "contact_id": row.contact_id, "account_id": getattr(found.account, "id", None),
            "enrollment_id": row.enrollment_id,
            "answered_message_id": getattr(found.answered, "id", None),
            "classification_id": getattr(classification, "id", None)}


async def _bounce(ts, mailbox, parsed) -> str:
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    ours = []
    for rfc_id in parsed.bounced_message_ids:
        message = await ts.first(EngagementMessage, EngagementMessage.rfc_message_id == rfc_id,
                                 EngagementMessage.direction == "out")
        if message is not None:
            ours.append(message)
    if not ours:
        return "discarded"          # a bounce about somebody else's email is not ours to keep
    for message in ours:
        message.status = "bounced"
        contact = await ts.get(Contact, message.contact_id) if message.contact_id else None
        address = (getattr(contact, "email", "") or (message.to_addrs or [""])[0])
        if address:
            await suppress(ts, email=address, reason="bounced", contact_id=message.contact_id,
                           source_message_id=message.id)
        if message.enrollment_id:
            enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
            if enrollment is not None and enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "bounced")
        await emit(ts, "message.bounced",
                   refs={"message_id": message.id, "contact_id": message.contact_id,
                         "mailbox_id": mailbox.id, "enrollment_id": message.enrollment_id},
                   payload={"recipients": parsed.bounced_recipients})
        await notify(ts, category="reply_bounced", owner_user_id=mailbox.owner_user_id,
                     title=f"{address} bounced", body="The address is now on do-not-contact.")
    await ts.flush()
    return "bounced"
