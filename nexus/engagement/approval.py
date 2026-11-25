# nexus/engagement/approval.py
"""What a person approved, bound to the exact email they were shown (owner decision 2026-11-25).

Sending a one-off email used to be one call: the composer, the orchestrator's tool, and nothing in
between. That was defensible while only a person could reach it. It stops being defensible once an
assistant can, because "send this" then means whatever the model decided the email should say.

So the send is split in two, the dry-run convention `mcp/tools/preview.py` already uses: a PREVIEW
renders exactly what would leave, and returns a short-lived token; the SEND presents that token.

**The token is bound to the CONTENT, not merely to the request.** Its payload carries a digest of
the recipient, the subject and the body as they were rendered. Change a word between the preview
and the send and the digest no longer matches, so the send is refused. That is the property worth
having: you cannot show a person one email and send another. A token alone, naming only the
contact, would have let a model preview a polite note and send something else to the same address.

What it does NOT prove is that a human read the preview — nothing reaching this process can. The
model is asked to show it and wait, the app shows it on screen, and the record afterwards says what
was approved, by which user's credentials, at what time, through which surface. That is an audit
trail, not consent, and `record_approval` is written to be read that way.

Short TTL for the same reason a checkout session has one: an approval is for the email in front of
you now, not a standing permission to send it tomorrow.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from jose import JWTError, jwt

from nexus.core.db import utcnow

#: An approval is for the email on screen now. Long enough to read a draft and think about it,
#: short enough that it cannot be banked.
TOKEN_TTL = timedelta(minutes=30)

TOKEN_TYP = "nexus.send.approval"

#: Where the approval came from, recorded verbatim on the message.
SURFACE_APP = "app"
SURFACE_MCP = "mcp"


class ApprovalError(RuntimeError):
    """The token is missing, expired, or does not describe the email being sent."""


def _secret() -> str:
    from nexus.core.config import get_settings

    return get_settings().secret_key


def _algorithm() -> str:
    from nexus.core.config import get_settings

    return get_settings().jwt_algorithm


def content_digest(*, to_addr: str, subject: str, body: str) -> str:
    """What the approval is FOR: the recipient and the words, as they would leave.

    Normalised only for trailing whitespace, which a text box adds and a mail client strips. Case
    is kept: "Re: pricing" and "RE: PRICING" are different subjects to a reader, and a preview that
    accepted either would be approving something other than what was shown.
    """
    parts = [
        (to_addr or "").strip().lower(),   # an address is case-insensitive; the words are not
        (subject or "").strip(),
        (body or "").strip(),
    ]
    joined = "\n\x1e".join(parts)          # a separator no email body contains
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def issue(*, tenant_id: str, user_id: str, contact_id: str, to_addr: str, subject: str,
          body: str, surface: str) -> str:
    """Sign a statement that this exact email was previewed for this user in this workspace."""
    now = utcnow()
    payload = {
        "typ": TOKEN_TYP,
        "tid": tenant_id,
        "sub": user_id,
        "cid": contact_id,
        "dig": content_digest(to_addr=to_addr, subject=subject, body=body),
        "srf": surface,
        "iat": int(now.timestamp()),
        "exp": int((now + TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_algorithm())


@dataclass(slots=True)
class Approval:
    """A verified approval, as it will be recorded."""

    user_id: str
    contact_id: str
    surface: str
    approved_at: object


def verify(token: str, *, tenant_id: str, user_id: str, contact_id: str, to_addr: str,
           subject: str, body: str) -> Approval:
    """Check the token approves EXACTLY this email, for this person, in this workspace.

    Every refusal says what to do, because each is a different mistake: nothing was approved, the
    approval has gone stale, somebody else approved it, or the email changed after it was approved.
    The last is the one worth the mechanism — it is what stops a previewed draft being swapped.
    """
    if not (token or "").strip():
        raise ApprovalError(
            "This email has not been approved. Ask for a preview first, show the person exactly "
            "what would be sent, and send only once they agree."
        )
    try:
        data = jwt.decode(token, _secret(), algorithms=[_algorithm()])
    except JWTError as exc:
        raise ApprovalError(
            "That approval has expired or could not be read. Preview the email again and ask the "
            "person to confirm it."
        ) from exc
    if data.get("typ") != TOKEN_TYP:
        raise ApprovalError("That is not an approval for sending an email.")
    if data.get("tid") != tenant_id:
        raise ApprovalError("That approval belongs to another workspace.")
    if data.get("sub") != user_id:
        raise ApprovalError(
            "That approval was given by somebody else. An approval is personal: the person sending "
            "must be the person who approved it."
        )
    if data.get("cid") != contact_id:
        raise ApprovalError("That approval was for a different contact.")
    if data.get("dig") != content_digest(to_addr=to_addr, subject=subject, body=body):
        raise ApprovalError(
            "The email has changed since it was approved. Preview it again and ask the person to "
            "confirm the new wording."
        )
    return Approval(user_id=user_id, contact_id=contact_id,
                    surface=str(data.get("srf") or ""), approved_at=utcnow())


async def record_approval(ts, *, contact_id: str, approval: Approval, subject: str,
                          body: str) -> None:
    """Write the approval onto the message this send produced.

    `EngagementMessage` already carries `approved_by_user_id` and `approved_at` — the campaign
    Review queue has written them since the engine shipped — so an approved one-off is retrieved
    exactly as an approved campaign step is, by the same columns, with no new table and no
    migration. That is the whole reason this does not invent an approvals table: one place to look.

    The SURFACE is logged rather than stored: a column for it is a migration, and the question a
    customer asks in a dispute ("who approved this, and when?") is answered by the two columns that
    already exist. Store it the day someone needs to query by it.

    Best effort. The email has left; failing the request now would tell the rep it did not.
    """
    import logging

    from sqlalchemy.exc import SQLAlchemyError

    from nexus.models.engagement import EngagementMessage

    logger = logging.getLogger("nexus.engagement.approval")
    # The row this send just wrote: newest outbound for the contact. `ts.first` takes no ordering,
    # so the statement is built here.
    stmt = (ts.select(EngagementMessage)
            .where(EngagementMessage.contact_id == contact_id,
                   EngagementMessage.direction == "out")
            .order_by(EngagementMessage.created_at.desc()))
    row = (await ts.session.scalars(stmt.limit(1))).first()
    if row is None:
        logger.warning("approved send for contact %s recorded no message to mark", contact_id)
        return
    try:
        row.approved_by_user_id = approval.user_id
        row.approved_at = approval.approved_at
        await ts.flush()
    except SQLAlchemyError:
        logger.warning("could not record the approval for contact %s", contact_id, exc_info=True)
        return
    logger.info("one-off email to contact %s approved by user %s via %s",
                contact_id, approval.user_id, approval.surface or "unknown")
