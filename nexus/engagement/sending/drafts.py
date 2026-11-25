"""Put a message in the SDR's own Drafts folder through Gmail or Microsoft Graph.

One function, because two screens do this — the contact composer's Save to Drafts and the reply
desk's — and a draft must be built exactly as a send would be: same From, same signature, same
opt-out footer and List-Unsubscribe headers. What the SDR opens in Gmail is what would have gone out.

**Never metered.** `outreach.email_send` prices a message that left the building; charging for a
draft bills a customer for pressing Save.

A draft that answers a message carries `In-Reply-To`/`References` and the provider's thread, so it
opens inside the conversation rather than as a new one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.engagement.sending.drafts")


async def save_draft(ts, *, mailbox, contact, subject: str, body: str, answering=None,
                     thread=None, kind: str = "oneoff") -> str:
    """Create the draft and return the provider's id for it.

    Raises the provider's errors. An expired grant also marks the mailbox ``needs_reauth``, so the
    next screen that shows it says "reconnect" rather than repeating a failure nobody can read.
    """
    from nexus.engagement.ids import new_ulid
    from nexus.engagement.mailboxes.provider import AuthExpired, ThreadRef
    from nexus.engagement.mailboxes.registry import open_provider
    from nexus.engagement.sending import mime
    from nexus.engagement.sending.service import _domain, _signature
    from nexus.engagement.suppression.tokens import unsubscribe_url

    parent_id = getattr(answering, "rfc_message_id", "") or ""
    # Held in variables rather than built inline, because the row written below has to carry the
    # SAME Message-ID as the draft the mailbox holds. That identity is the whole point: see
    # `_record_draft`.
    rfc_message_id = mime.new_rfc_message_id(_domain(mailbox.email))
    ref = new_ulid()
    references = mime.references_for(getattr(answering, "references_header", "") or "", parent_id)
    message = mime.build_message(
        from_addr=mailbox.email, from_name=mailbox.display_name or "", to_addr=contact.email,
        subject=(subject or "").strip(), body=body,
        message_id=rfc_message_id, ref=ref,
        unsubscribe_url=unsubscribe_url(ts.tenant_id, contact.id),
        unsubscribe_mailto=mailbox.email, signature=await _signature(ts, mailbox),
        in_reply_to=parent_id,
        references=references)
    thread_ref = None
    if thread is not None:
        thread_ref = ThreadRef(
            provider_thread_id=thread.provider_thread_id or "",
            reply_to_provider_message_id=getattr(answering, "provider_message_id", "") or "",
            in_reply_to=parent_id)
    try:
        provider = await open_provider(ts, mailbox)
        draft_id = await provider.create_draft(mime.to_bytes(message), thread=thread_ref)
    except AuthExpired:
        mailbox.status = "needs_reauth"
        mailbox.last_error = mailbox.last_error or "Reconnect this mailbox: the grant expired"
        await ts.flush()
        raise
    await _record_draft(
        ts, mailbox=mailbox, contact=contact, thread=thread, kind=kind, draft_id=draft_id,
        rfc_message_id=rfc_message_id, ref=ref, in_reply_to=parent_id, references=references,
        subject=(subject or "").strip(), body=body,
    )
    return draft_id


async def _record_draft(ts, *, mailbox, contact, thread, kind: str, draft_id: str,
                        rfc_message_id: str, ref: str, in_reply_to: str, references: str,
                        subject: str, body: str) -> None:
    """Record the draft as an outbound message, so a reply to it can be matched.

    The identifiers a reply is matched on were MINTED here and thrown away. `save_draft` built a
    Message-ID, handed it to the provider and returned the draft id, so once the rep pressed Send
    in Gmail the conversation existed in their mailbox and nowhere else.

    `replies/matching.py` keeps an inbound email only when it matches a known thread, an
    `In-Reply-To`/`References` naming one of OUR `rfc_message_id`s, or a contact with a live
    enrollment; anything else is discarded unread, subject and body included. A one-off email —
    the composer's Save to Drafts, the reply desk's, and every `save_email_draft` an assistant
    makes over MCP — has no enrollment, so rule 2 was the only rule that could have caught its
    reply, and the id it needed existed for the length of this function.

    `status="drafted"`, which is deliberately NOT one of `desk/inbox.SHOWN`: nothing has been sent,
    so the message must not appear in Mail, be counted as a touch, or read as a sent step. It is
    the identity, kept.

    Never metered — the module docstring's rule, unchanged: a draft bills nobody.

    Never raises. The draft is already in the rep's mailbox; failing the request after that would
    leave them an email on screen that the product says it could not save.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from nexus.models.engagement import EngagementMessage

    try:
        async with ts.session.begin_nested():
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id,
                thread_id=getattr(thread, "id", None),
                # No enrollment and no step: a draft belongs to a person, not to a sequence
                # position, and the partial unique index on (enrollment_id, step_index) counts
                # NULLs as distinct, so several drafts to one person do not collide.
                enrollment_id=None,
                contact_id=getattr(contact, "id", None),
                direction="out",
                kind=kind,
                status="drafted",
                rfc_message_id=rfc_message_id,
                ref_header=ref,
                provider_message_id=draft_id or None,
                in_reply_to=in_reply_to,
                references_header=references,
                from_addr=mailbox.email,
                to_addrs=[contact.email] if getattr(contact, "email", "") else [],
                subject=subject,
                body_text=body,
            ))
            await ts.flush()
    except SQLAlchemyError:
        logger.warning("could not record the draft for contact %s",
                       getattr(contact, "id", "?"), exc_info=True)
