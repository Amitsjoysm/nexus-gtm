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

    owned = [
        a for a in _accounts(tenant)
        if a.get("owner_user_id") == user_id and a.get("enabled", True)
    ]
    if not owned:
        raise MailboxNotConnected(
            "You have not connected a sending mailbox. Add yours under Settings -> Sending "
            "mailboxes, send yourself a test, then try again."
        )
    usable = [a for a in owned if account_is_configured(a)]
    if not usable:
        raise MailboxNotConnected(
            "Your mailbox is missing its app password, so it cannot send. Re-enter it under "
            "Settings -> Sending mailboxes."
        )
    # Their default first if they marked one, else the first they connected. Stable either way:
    # which of a rep's own mailboxes sends must not vary run to run.
    usable.sort(key=lambda a: (not a.get("default", False), a.get("id", "")))
    return usable[0]


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
    from nexus.integrations.email_sender import send_email
    from nexus.models.identity import Tenant

    to = (getattr(contact, "email", "") or "").strip()
    if not to:
        raise SendRefused("This contact has no email address.")
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

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    mailbox = resolve_rep_mailbox(tenant, user_id)

    result = await send_email(
        {"accounts": [mailbox]}, to=to, subject=(subject or "").strip(), body=body
    )
    ok = bool(getattr(result, "ok", False))
    detail = str(getattr(result, "detail", "") or ("sent" if ok else "send failed"))
    if ok:
        await _meter_send(ts, user_id=user_id)
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
