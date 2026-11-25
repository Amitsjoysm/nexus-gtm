# nexus/outreach/send.py
"""Send one drafted email to one contact, from the rep's own mailbox.

The product could draft a hyper-personalised email and could not send it. `EmailComposer` offered
Regenerate, Copy, and a `mailto:` link that dumps the draft into Outlook, so the rep's last step was
copy-and-paste. Meanwhile every piece needed already existed and was referenced by nothing:

* `Tenant.email_settings["accounts"]` — per-mailbox SMTP a workspace configures in Settings, with
  a working Send-test button. Referenced ONLY by its own CRUD router.
* `integrations/email_sender.send_email()` — a real sender, used only for test messages.
* `outreach.email_send` — catalogued and priced, metered only on the orchestrator's SEP path.

Built, stored and unreachable, three times over, in the one place the product is named for.

**A mailbox belongs to a rep, and sending requires your own.** Attribution is the reason: a reply
goes back to whoever sent it, and a shared workspace address turns every reply into a triage
problem. `owner_user_id` lives in the same JSON blob as the rest of the mailbox, so this needs no
migration; a mailbox created before ownership existed has no owner and is deliberately NOT usable
for sending, because silently sending as somebody else is worse than asking the rep to connect one.

**The rep's click is the review.** The product's "nothing sends until you approve it" rule governs
AUTOMATED outreach — the orchestrator's `SendMessageTool` still carries `requires_approval` and
cadences still run through the gate. A human who read the draft, edited it, and pressed Send has
performed that review; adding a second gate would make "one click" untrue without making anything
safer.

**An unverified address warns, it does not block.** The rep decides, because the verifier returns
`unknown` far more often than it returns anything else and a hard block would stop most real sends.
`invalid` is the exception a caller must accept explicitly: those bounce, and bounces cost the
sending domain its ability to deliver anything at all.

**A mailbox connected with Google or Microsoft sends first.** A rep who pressed Connect Google on
My mailboxes expects the composer to use it. It used to read only the SMTP app-password entries in
`Tenant.email_settings`, so that rep was told "you have not connected a sending mailbox" beside the
mailbox they had just connected. The OAuth mailbox now sends through `engagement.sending.send` —
the provider API, the do-not-contact list, the exactly-once row and the meter, the same path a
campaign step takes — and saves drafts into the provider's own Drafts folder. An SMTP mailbox is
the fallback for a rep who has none, and keeps working exactly as before.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("nexus.outreach.send")

#: Verifier verdicts that bounce. Sending to one damages the domain's reputation for every later
#: email, which is why it takes an explicit acknowledgement rather than a shrug.
RISKY_STATUSES = ("invalid",)


class SendRefused(RuntimeError):
    """The send was refused for a reason the caller can fix and should be told about."""


class MailboxNotConnected(SendRefused):
    """This rep has no sending mailbox of their own."""


@dataclass(slots=True)
class SendOutcome:
    ok: bool
    detail: str
    mailbox_id: str = ""
    from_email: str = ""
    to: str = ""
    #: The verifier's verdict at send time, echoed so the caller can record what was known.
    email_status: str = ""


def _accounts(tenant) -> list[dict]:
    settings = tenant.email_settings or {}
    accounts = settings.get("accounts")
    return [dict(a) for a in accounts] if isinstance(accounts, list) else []


def resolve_rep_mailbox(tenant, user_id: str) -> dict:
    """The mailbox this user sends from. Raises `MailboxNotConnected` when they have none.

    Only their OWN, enabled, and actually configured — an entry with no password cannot send, and
    reporting "sent" for one would be the silent failure this module exists to remove.
    """
    from nexus.integrations.email_sender import account_is_configured

    accounts = _accounts(tenant)
    mine = [a for a in accounts if a.get("owner_user_id") == user_id]
    usable = [a for a in mine if a.get("enabled", True) and account_is_configured(a)]
    if not usable:
        # NAME THE ACTUAL STATE. All four of these used to read "You have not connected a sending
        # mailbox", which is false for three of them and sends the rep to add a second mailbox
        # nobody will use. Reported 2026-09-16 as "even after connecting email, SMTP not connected".
        unowned = [a for a in accounts if not (a.get("owner_user_id") or "")]
        if mine and any(not a.get("enabled", True) for a in mine):
            raise MailboxNotConnected(
                "Your mailbox is switched off, so it cannot send. Turn sending on for it under "
                "Settings -> Sending mailboxes."
            )
        if mine:
            raise MailboxNotConnected(
                "Your mailbox is missing its app password, so it cannot send. Re-enter it under "
                "Settings -> Sending mailboxes, then press Test connection."
            )
        if unowned:
            labels = ", ".join(
                str(a.get("label") or a.get("from_email") or a.get("username") or "a mailbox")
                for a in unowned[:3]
            )
            raise MailboxNotConnected(
                f"This workspace has a mailbox nobody owns ({labels}). Claim it under Settings -> "
                "Sending mailboxes to send from it, or add your own."
            )
        if accounts:
            raise MailboxNotConnected(
                "The mailboxes in this workspace belong to colleagues, and a reply goes back to "
                "whoever sent the email. Add your own under Settings -> Sending mailboxes."
            )
        raise MailboxNotConnected(
            "You have not connected a sending mailbox. Connect Gmail or Outlook under My "
            "mailboxes, or add an SMTP mailbox under Settings -> Sending mailboxes."
        )
    # Their default first if they marked one, else the first they connected. Stable either way:
    # which of a rep's own mailboxes sends must not vary run to run.
    usable.sort(key=lambda a: (not a.get("default", False), a.get("id", "")))
    return usable[0]


#: `MailboxConnection.provider` → how the screen names it.
_PROVIDER_LABELS = {"google": "Gmail", "microsoft": "Outlook"}


async def resolve_connected_mailbox(ts, user_id: str):
    """The rep's own mailbox connected with Google or Microsoft and able to send, or ``None``.

    The first one they connected, so which mailbox sends does not vary between two clicks.
    """
    from nexus.models.engagement import MailboxConnection

    rows = await ts.list(MailboxConnection, MailboxConnection.owner_user_id == user_id,
                         MailboxConnection.status == "connected")
    rows.sort(key=lambda r: (r.created_at, r.id))
    return rows[0] if rows else None


async def _smtp_mailbox(ts, tenant, user_id: str) -> dict:
    """The rep's SMTP mailbox, or a refusal that names the real problem.

    Reached only when the rep has no working OAuth mailbox. If they have one that stopped working,
    that is what they need to hear about: "add an SMTP mailbox" would send them to set up a second
    mailbox when the fix is one click on the one they already have.
    """
    from nexus.models.engagement import MailboxConnection

    try:
        return resolve_rep_mailbox(tenant, user_id)
    except MailboxNotConnected:
        stale = [r for r in await ts.list(MailboxConnection,
                                          MailboxConnection.owner_user_id == user_id)
                 if r.status in ("needs_reauth", "error")]
        if stale:
            box = stale[0]
            label = _PROVIDER_LABELS.get(box.provider, box.provider)
            raise MailboxNotConnected(
                f"Your {label} mailbox {box.email} needs reconnecting before it can send. "
                "Reconnect it in Settings, My mailboxes, then try again."
            ) from None
        raise


async def _send_through_connection(ts, *, connection, contact, to: str, subject: str, body: str,
                                   user_id: str, email_status: str) -> SendOutcome:
    """Send through Gmail or Graph by the engine's own sender.

    That sender checks the do-not-contact list, writes the outbound row before the provider call,
    meters `outreach.email_send` inside the call and records the thread, so a reply to a one-off
    email lands on the reply desk like any other. A refusal it would STOP on (the address is
    blocked) is the caller's to hear about; a HOLD (paused mailbox, expired grant, no credits) is a
    failed send with the reason.
    """
    from nexus.engagement.sending.service import send

    result = await send(ts, mailbox=connection, contact=contact, subject=(subject or "").strip(),
                        body=body, kind="oneoff", user_id=user_id)
    if result.outcome == "stopped":
        raise SendRefused(f"Not sent: {result.reason}.")
    ok = result.sent
    if not ok:
        logger.warning("one-off send to %s through %s was %s: %s", to, connection.provider,
                       result.outcome, result.reason)
    return SendOutcome(
        ok=ok,
        detail="sent" if ok else (result.reason or "send failed"),
        mailbox_id=connection.id,
        from_email=connection.email,
        to=to,
        email_status=email_status,
    )


async def _draft_through_connection(ts, *, connection, contact, to: str, subject: str,
                                    body: str) -> SendOutcome:
    from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError
    from nexus.engagement.sending.drafts import save_draft

    try:
        await save_draft(ts, mailbox=connection, contact=contact, subject=subject, body=body)
    except AuthExpired:
        detail = "the mailbox needs reconnecting (Settings, My mailboxes)"
        ok = False
    except ProviderError as exc:
        detail = f"could not save: {exc}"
        ok = False
    else:
        detail, ok = "saved to drafts", True
    if not ok:
        logger.warning("draft for %s through %s could not be saved: %s", to,
                       connection.provider, detail)
    return SendOutcome(
        ok=ok,
        detail=detail,
        mailbox_id=connection.id,
        from_email=connection.email,
        to=to,
        email_status=(getattr(contact, "email_status", "") or "").strip().lower(),
    )


def _one_address_or_refuse(contact) -> str:
    """The contact's one address, or a refusal: no address, or a field holding several, which
    would put every one of them in To: (2026-11-20, `contacts.addresses`)."""
    from nexus.contacts.addresses import single_address

    raw = (getattr(contact, "email", "") or "").strip()
    if not raw:
        raise SendRefused("This contact has no email address.")
    to = single_address(raw)
    if to is None:
        raise SendRefused("This contact's email field is not one address. Correct it on the "
                          "contact first.")
    return to


async def send_to_contact(
    ts,
    *,
    contact,
    subject: str,
    body: str,
    user_id: str,
    allow_risky: bool = False,
) -> SendOutcome:
    """Send `subject`/`body` to `contact` from `user_id`'s own mailbox.

    Metering happens AFTER the send and never raises, mirroring `orchestration.tools._meter_send`:
    a message that has already left cannot be un-sent by a billing error, and metering must never
    be the reason an outbound email fails.
    """
    from nexus.engagement.suppression.service import active_block
    from nexus.integrations.email_sender import send_email
    from nexus.models.identity import Tenant

    to = _one_address_or_refuse(contact)
    if not (body or "").strip():
        # The blank-draft guard in `agents/messaging.py` exists for the same reason: an empty
        # message is not a message, and sending one is worse than refusing to.
        raise SendRefused("The draft is empty. Generate or write a body before sending.")

    status = (getattr(contact, "email_status", "") or "").strip().lower()
    if status in RISKY_STATUSES and not allow_risky:
        raise SendRefused(
            f"This address was verified as {status}, so it will almost certainly bounce, and "
            "bounces cost your domain its ability to deliver anything. Send anyway only if you "
            "know the address is good."
        )

    connection = await resolve_connected_mailbox(ts, user_id)
    if connection is not None:
        return await _send_through_connection(
            ts, connection=connection, contact=contact, to=to, subject=subject, body=body,
            user_id=user_id, email_status=status)

    # The do-not-contact list binds an app-password mailbox too. Only the connected path checked it
    # (through the engine's sender), so an unsubscribed buyer could be emailed again from Settings
    # -> Sending mailboxes (2026-11-20). Checked before the mailbox, as that path does.
    block = await active_block(ts, to)
    if block is not None:
        raise SendRefused(f"Not sent: {to} is on the do-not-contact list ({block.reason}).")

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    mailbox = await _smtp_mailbox(ts, tenant, user_id)

    # Sign it. The composer shows the signature too, so `append_signature` is idempotent — without
    # that the buyer reads the rep's phone number twice.
    from nexus.outreach.signature import append_signature, resolve_signature

    signed = append_signature(body, resolve_signature(tenant.email_settings, mailbox))

    # THE MAILBOX ITSELF, not `{"accounts": [mailbox]}`.
    #
    # `resolve_smtp` reads `provider`, `host`, `username` and `password` off the TOP LEVEL of what
    # it is handed. Wrapped in an accounts list, every one of them is a level down, so the resolved
    # host was empty and `send_email` answered "smtp not configured" before opening a socket —
    # while the Send-test button, which passes the mailbox dict, worked. Reported 2026-09-16 as
    # "send test successful, sending to a contact says mailbox not configured".
    result = await send_email(mailbox, to=to, subject=(subject or "").strip(), body=signed,
                              guard_host=True)
    ok = bool(getattr(result, "ok", False))
    detail = str(getattr(result, "detail", "") or ("sent" if ok else "send failed"))
    if ok:
        await _meter_send(ts, user_id=user_id)
        await _record_smtp_send(
            ts, contact=contact, to=to, subject=(subject or "").strip(), body=signed,
            mailbox=mailbox)
    else:
        # The SMTP error, not a generic failure: "authentication failed" and "recipient rejected"
        # send a rep to two completely different places.
        logger.warning("outreach send to %s failed: %s", to, detail)

    return SendOutcome(
        ok=ok,
        detail=detail,
        mailbox_id=str(mailbox.get("id", "")),
        from_email=str(mailbox.get("from_email") or mailbox.get("username") or ""),
        to=to,
        email_status=status,
    )


async def _record_smtp_send(ts, *, contact, to: str, subject: str, body: str,
                            mailbox: dict) -> None:
    """Record a send from an app-password mailbox as an outbound message.

    The connected (Gmail/Graph) path goes through `engagement.sending.send`, which writes this row
    before the provider call, records the thread and meters. The SMTP path did neither: it metered
    and returned, so the email existed in the rep's mail client and nowhere else.

    What that cost was not a report. `replies/matching.py` keeps an inbound email only when it
    matches a known thread, an `In-Reply-To`/`References` naming one of OUR `rfc_message_id`s, or a
    contact with a live enrollment — and a one-off has no enrollment. With no row there was no id
    to match, so the buyer's reply was discarded unread. The rep saw it; the product did not.

    `mailbox_connection_id` is NULL here (migration 0080): an app-password mailbox is a dict in
    `tenants.email_settings` and has no row to point at. `rfc_message_id` is minted the same way
    the engine mints one, because matching a reply is the entire reason the row exists.

    Never raises. The email has left, and failing the request now would tell the rep it had not.
    """
    import logging as _logging

    from sqlalchemy.exc import SQLAlchemyError

    from nexus.core.db import utcnow
    from nexus.engagement.ids import new_ulid
    from nexus.engagement.sending import mime
    from nexus.models.engagement import EngagementMessage

    log = _logging.getLogger("nexus.outreach.send")
    sender = str(mailbox.get("from_email") or mailbox.get("username") or "")
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "localhost"
    try:
        async with ts.session.begin_nested():
            ts.add(EngagementMessage(
                mailbox_connection_id=None,
                thread_id=None,
                enrollment_id=None,
                contact_id=getattr(contact, "id", None),
                direction="out",
                kind="oneoff",
                status="sent",
                rfc_message_id=mime.new_rfc_message_id(domain),
                ref_header=new_ulid(),
                from_addr=sender,
                to_addrs=[to],
                subject=subject,
                body_text=body,
                sent_at=utcnow(),
            ))
            await ts.flush()
    except SQLAlchemyError:
        log.warning("could not record the SMTP send to %s", to, exc_info=True)


@dataclass(slots=True)
class SendPreview:
    """What the recipient will actually read, before anything is sent."""

    to: str
    from_email: str
    from_name: str
    subject: str
    #: Body AND signature, composed exactly as the send composes them.
    body: str
    #: Which mailbox would send it, for the sentence the screen shows.
    mailbox_kind: str          # connected | smtp
    signed: bool


async def preview_for_contact(ts, *, contact, subject: str, body: str,
                              user_id: str) -> SendPreview:
    """Render the email the send would produce. Sends nothing, charges nothing.

    The composer shows the body the rep wrote and then says "your signature is added when this
    goes out" — an admission that what is on screen is not what the buyer receives. A rep
    approving an email they have not fully seen is approving a guess, and the signature is the
    part that carries their real name, title and phone number.

    **It shares the send's own resolution rather than reproducing it**, which is the only way a
    preview stays true: `resolve_connected_mailbox` first, then the SMTP mailbox, then the same
    composer each path uses (`compose_message_text` for a connected mailbox, `append_signature`
    for SMTP). Reproducing the branching here would be a second source of truth for what the buyer
    reads, and the first thing to drift would be the signature — the exact thing this exists to
    show.

    Deliberately NOT applied here: the risky-address refusal and the do-not-contact block. Both
    belong to sending, and a preview that refuses to render would leave a rep guessing at wording
    they are allowed to fix. The send still refuses on its own.
    """
    from nexus.models.identity import Tenant

    to = _one_address_or_refuse(contact)
    if not (body or "").strip():
        raise SendRefused("The draft is empty. Generate or write a body before previewing it.")

    connection = await resolve_connected_mailbox(ts, user_id)
    if connection is not None:
        from nexus.engagement.sending.mime import compose_message_text
        from nexus.engagement.sending.service import _signature

        signature = await _signature(ts, connection)
        sender = connection.display_name or ""
        return SendPreview(
            to=to, from_email=connection.email, from_name=sender,
            subject=(subject or "").strip(),
            body=compose_message_text(body, signature, sender),
            mailbox_kind="connected", signed=bool(signature),
        )

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    mailbox = await _smtp_mailbox(ts, tenant, user_id)

    from nexus.outreach.signature import append_signature, resolve_signature

    signature = resolve_signature(tenant.email_settings, mailbox)
    return SendPreview(
        to=to,
        from_email=str(mailbox.get("from_email") or mailbox.get("username") or ""),
        from_name=str(mailbox.get("from_name") or ""),
        subject=(subject or "").strip(),
        body=append_signature(body, signature),
        mailbox_kind="smtp", signed=bool(signature),
    )


async def draft_for_contact(
    ts,
    *,
    contact,
    subject: str,
    body: str,
    user_id: str,
) -> SendOutcome:
    """Put the draft in the rep's own Drafts folder instead of sending it.

    Same mailbox, same signature, same message — it just stops one step short. `save_to_drafts`
    (IMAP APPEND) has existed since the sender did and was reachable from the orchestrator's tool
    alone, so a rep reading a draft on screen had one button that did anything with it.

    Two deliberate differences from `send_to_contact`:

    * **Nothing is metered.** `outreach.email_send` prices a message that left the building, and
      charging for a draft would bill a customer for pressing save.
    * **A doubtful address does not block it.** The message goes into the rep's own mailbox for them
      to look at; refusing that protects nobody, and the send path still refuses on its own.
    """
    from nexus.integrations.email_sender import has_drafts_support, save_to_drafts
    from nexus.models.identity import Tenant

    to = _one_address_or_refuse(contact)
    if not (body or "").strip():
        raise SendRefused("The draft is empty. Generate or write a body before saving it.")

    connection = await resolve_connected_mailbox(ts, user_id)
    if connection is not None:
        return await _draft_through_connection(
            ts, connection=connection, contact=contact, to=to, subject=subject, body=body)

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    mailbox = await _smtp_mailbox(ts, tenant, user_id)
    if not has_drafts_support(mailbox):
        # "Saved" for something nobody can find is worse than a refusal naming the missing field.
        # Only the known providers carry an IMAP host in their preset; a custom SMTP mailbox has
        # one only if somebody typed it under Settings -> Sending mailboxes -> Server settings.
        raise SendRefused(
            "This mailbox has no IMAP host, so it cannot hold drafts. Add one under Settings -> "
            "Sending mailboxes -> Server settings, or send the email directly."
        )

    from nexus.outreach.signature import append_signature, resolve_signature

    signed = append_signature(body, resolve_signature(tenant.email_settings, mailbox))

    # THE MAILBOX ITSELF, for the reason spelled out in `send_to_contact`: `resolve_smtp` reads the
    # host, username and password off the TOP LEVEL of what it is handed.
    result = await save_to_drafts(mailbox, to=to, subject=(subject or "").strip(), body=signed,
                                  guard_host=True)
    ok = bool(getattr(result, "ok", False))
    detail = str(getattr(result, "detail", "") or ("saved to drafts" if ok else "could not save"))
    if not ok:
        logger.warning("draft for %s could not be saved: %s", to, detail)

    return SendOutcome(
        ok=ok,
        detail=detail,
        mailbox_id=str(mailbox.get("id", "")),
        from_email=str(mailbox.get("from_email") or mailbox.get("username") or ""),
        to=to,
        email_status=(getattr(contact, "email_status", "") or "").strip().lower(),
    )


async def _meter_send(ts, *, user_id: str) -> None:
    """Charge one `outreach.email_send`. Never raises.

    `metered`, not `record_usage`: the latter records the send without charging for it. Catalogued
    and priced since the billing milestone and metered only on the orchestrator's sequence path, so
    a rep sending by hand was free.
    """
    from nexus.billing.meter import metered

    try:
        async with metered(
            ts, "outreach.email_send", quantity=1, user_id=user_id, source="api"
        ):
            pass
    except Exception:
        logger.warning("metering failed for outreach.email_send", exc_info=True)
