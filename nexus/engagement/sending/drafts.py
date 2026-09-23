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


async def save_draft(ts, *, mailbox, contact, subject: str, body: str, answering=None,
                     thread=None) -> str:
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
    message = mime.build_message(
        from_addr=mailbox.email, from_name=mailbox.display_name or "", to_addr=contact.email,
        subject=(subject or "").strip(), body=body,
        message_id=mime.new_rfc_message_id(_domain(mailbox.email)), ref=new_ulid(),
        unsubscribe_url=unsubscribe_url(ts.tenant_id, contact.id),
        unsubscribe_mailto=mailbox.email, signature=await _signature(ts, mailbox),
        in_reply_to=parent_id,
        references=mime.references_for(getattr(answering, "references_header", "") or "",
                                       parent_id))
    thread_ref = None
    if thread is not None:
        thread_ref = ThreadRef(
            provider_thread_id=thread.provider_thread_id or "",
            reply_to_provider_message_id=getattr(answering, "provider_message_id", "") or "",
            in_reply_to=parent_id)
    try:
        provider = await open_provider(ts, mailbox)
        return await provider.create_draft(mime.to_bytes(message), thread=thread_ref)
    except AuthExpired:
        mailbox.status = "needs_reauth"
        mailbox.last_error = mailbox.last_error or "Reconnect this mailbox: the grant expired"
        await ts.flush()
        raise
