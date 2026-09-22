"""The do-not-contact list (D7, D11, spec §4, §9).

**Keyed by the normalised address, not by the contact.** The same person can sit under two accounts
or come back in next quarter's import; a block on one contact row would miss the others.

Four reasons, and the two that matter behave differently:

* ``unsubscribed`` — permanent. Nobody lifts it: a person who asked to stop receiving email must not
  be emailed again because a colleague disagreed (CAN-SPAM, and the one-click promise in the
  header).
* ``declined`` — a clear "no" (D7). Blocks everywhere until an SDR or manager lifts it with a note,
  which is audited, so "why did we email her again?" has an answer.
* ``bounced`` and ``manual`` — liftable the same way.

``suppress`` is idempotent: blocking an address that is already blocked returns the existing row, and
an ``unsubscribed`` request upgrades an existing ``declined`` block so it becomes permanent.

Stopping the enrollments of a newly blocked address is added in phase 08, where enrollments exist.
"""
from __future__ import annotations

from nexus.core.audit import record_audit
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import DNC_REASONS, DoNotContact

PERMANENT = frozenset({"unsubscribed"})


class PermanentBlock(ValueError):
    """An unsubscribe cannot be lifted."""


def normalize_email(address: str | None) -> str:
    return (address or "").strip().lower()


async def active_block(ts: TenantSession, email: str) -> DoNotContact | None:
    address = normalize_email(email)
    if not address:
        return None
    return await ts.first(DoNotContact, DoNotContact.email == address,
                          DoNotContact.lifted_at.is_(None))


async def active_reasons(ts: TenantSession, emails: list[str]) -> dict[str, str]:
    """``{address: reason}`` for every blocked address among ``emails`` (one query)."""
    addresses = sorted({normalize_email(e) for e in emails if normalize_email(e)})
    if not addresses:
        return {}
    rows = await ts.list(DoNotContact, DoNotContact.email.in_(addresses),
                         DoNotContact.lifted_at.is_(None))
    return {row.email: row.reason for row in rows}


async def suppress(
    ts: TenantSession, *, email: str, reason: str, contact_id: str | None = None,
    source_message_id: str | None = None, created_by_user_id: str | None = None,
) -> DoNotContact:
    if reason not in DNC_REASONS:
        raise ValueError(f"unknown do-not-contact reason {reason!r}")
    address = normalize_email(email)
    if "@" not in address:
        raise ValueError("a do-not-contact entry needs an email address")
    existing = await active_block(ts, address)
    if existing is not None:
        if reason in PERMANENT and existing.reason not in PERMANENT:
            existing.reason = reason
            existing.source_message_id = source_message_id or existing.source_message_id
            await ts.flush()
        return existing
    block = DoNotContact(email=address, reason=reason, contact_id=contact_id,
                         source_message_id=source_message_id,
                         created_by_user_id=created_by_user_id)
    ts.add(block)
    await ts.flush()
    await record_audit(ts, "engagement.dnc.add", actor_user_id=created_by_user_id,
                       target_type="do_not_contact", target_id=block.id,
                       meta={"reason": reason})
    from nexus.engagement.ledger.emit import emit

    await emit(ts, "contact.suppressed", actor_user_id=created_by_user_id,
               refs={"contact_id": contact_id, "message_id": source_message_id},
               payload={"reason": reason})
    return block


async def lift(ts: TenantSession, block: DoNotContact, *, user_id: str, note: str) -> DoNotContact:
    if block.reason in PERMANENT:
        raise PermanentBlock("An unsubscribe is permanent and cannot be lifted.")
    if block.lifted_at is not None:
        return block
    text = (note or "").strip()
    if len(text) < 5:
        raise ValueError("Say why you are lifting this block; the note is kept with the record.")
    block.lifted_at = utcnow()
    block.lifted_by_user_id = user_id
    block.lift_note = text[:2000]
    await ts.flush()
    await record_audit(ts, "engagement.dnc.lift", actor_user_id=user_id,
                       target_type="do_not_contact", target_id=block.id,
                       meta={"reason": block.reason})
    from nexus.engagement.ledger.emit import emit

    await emit(ts, "contact.unsuppressed", actor_user_id=user_id,
               refs={"contact_id": block.contact_id},
               payload={"reason": block.reason, "note": block.lift_note})
    return block


async def list_blocks(
    ts: TenantSession, *, q: str = "", include_lifted: bool = False, limit: int = 100,
    offset: int = 0,
) -> list[DoNotContact]:
    where = [] if include_lifted else [DoNotContact.lifted_at.is_(None)]
    needle = normalize_email(q)
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(DoNotContact.email.like(f"%{escaped}%", escape="\\"))
    stmt = (ts.select(DoNotContact, *where).order_by(DoNotContact.created_at.desc())
            .offset(max(0, offset)).limit(max(1, min(limit, 500))))
    return list((await ts.session.scalars(stmt)).all())
