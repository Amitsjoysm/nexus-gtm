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

**A whole domain can be blocked too** (2026-09-30), stored in the same column as ``@acme.io`` so it
shares the one-active-block index and needs no migration. Every READ is domain-aware: an address is
blocked by its own row or by a row for its domain or any parent domain (``sam@eu.acme.io`` by
``@acme.io``). ``suppress`` itself matches the exact key only, because one person unsubscribing
under a blocked domain must not turn the whole domain's block permanent.

Stopping the enrollments of a newly blocked address is added in phase 08, where enrollments exist.
"""
from __future__ import annotations

import re

from nexus.core.audit import record_audit
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import DNC_REASONS, DoNotContact

PERMANENT = frozenset({"unsubscribed"})


class PermanentBlock(ValueError):
    """An unsubscribe cannot be lifted."""


#: One DNS label: letters, digits and inner hyphens.
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
_HOST = re.compile(r"^[a-z0-9.-]{3,253}$")


def normalize_email(address: str | None) -> str:
    return (address or "").strip().lower()


def normalize_domain(raw: str | None) -> str:
    """``acme.io`` from ``acme.io``, ``@Acme.io``, ``www.acme.io`` or ``https://acme.io/about``;
    ``""`` when it is not a domain. A bare label is refused: blocking ``com`` blocks the world."""
    text = (raw or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text).lstrip("@")
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    labels = text.split(".")
    if len(labels) < 2 or len(text) > 253 or not all(_LABEL.match(label) for label in labels):
        return ""
    if labels[-1].isdigit():
        return ""
    return text


def classify_entry(raw: str | None) -> tuple[str | None, str]:
    """``("email", address)``, ``("domain", domain)`` or ``(None, raw)`` for one line of an upload."""
    text = (raw or "").strip().strip("\"'<>,;").strip()
    if "@" in text and not text.startswith("@"):
        local, _, host = text.lower().rpartition("@")
        # The host as written, not normalised: `www.` is stripped from a DOMAIN entry because that
        # is a website someone pasted, but in an address it is where the mail goes.
        if _LOCAL.match(local) and _HOST.match(host) and normalize_domain(host):
            return "email", f"{local}@{host}"
        return None, text
    domain = normalize_domain(text)
    return ("domain", domain) if domain else (None, text)


def _keys_for(address: str) -> list[str]:
    """The rows that can block ``address``: itself, then its domain and each parent, most specific
    first. ``sam@eu.acme.io`` -> ``sam@eu.acme.io``, ``@eu.acme.io``, ``@acme.io``."""
    if "@" not in address or address.startswith("@"):
        return [address] if address else []
    labels = address.rpartition("@")[2].split(".")
    return [address] + ["@" + ".".join(labels[i:]) for i in range(len(labels) - 1)]


async def active_block(ts: TenantSession, email: str) -> DoNotContact | None:
    """The block that stops ``email`` being contacted: its own, else its domain's."""
    keys = _keys_for(normalize_email(email))
    if not keys:
        return None
    rows = {row.email: row for row in await ts.list(
        DoNotContact, DoNotContact.email.in_(keys), DoNotContact.lifted_at.is_(None))}
    return next((rows[k] for k in keys if k in rows), None)


async def active_reasons(ts: TenantSession, emails: list[str]) -> dict[str, str]:
    """``{address: reason}`` for every blocked address among ``emails`` (one query), counting a
    block on the address's domain."""
    addresses = sorted({normalize_email(e) for e in emails if normalize_email(e)})
    if not addresses:
        return {}
    keys_by_address = {a: _keys_for(a) for a in addresses}
    wanted = sorted({k for keys in keys_by_address.values() for k in keys})
    rows = {row.email: row.reason for row in await ts.list(
        DoNotContact, DoNotContact.email.in_(wanted), DoNotContact.lifted_at.is_(None))}
    out: dict[str, str] = {}
    for address, keys in keys_by_address.items():
        hit = next((k for k in keys if k in rows), None)
        if hit is not None:
            out[address] = rows[hit]
    return out


async def _exact_block(ts: TenantSession, key: str) -> DoNotContact | None:
    return await ts.first(DoNotContact, DoNotContact.email == key,
                          DoNotContact.lifted_at.is_(None))


async def suppress(
    ts: TenantSession, *, email: str, reason: str, contact_id: str | None = None,
    source_message_id: str | None = None, created_by_user_id: str | None = None,
) -> DoNotContact:
    if reason not in DNC_REASONS:
        raise ValueError(f"unknown do-not-contact reason {reason!r}")
    address = normalize_email(email)
    if "@" not in address:
        raise ValueError("a do-not-contact entry needs an email address")
    # The exact key only: a domain's block is not this address's row, and upgrading it to
    # `unsubscribed` below would make the whole domain permanent over one person.
    existing = await _exact_block(ts, address)
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


async def block_entry(
    ts: TenantSession, raw: str, *, created_by_user_id: str | None = None,
) -> tuple[DoNotContact, bool]:
    """Block one typed or uploaded entry, an address or a domain, as a manual block. Returns the
    active row and whether it is new. Raises ``ValueError`` for an entry that is neither."""
    kind, value = classify_entry(raw)
    if kind is None:
        raise ValueError("Enter an email address (name@company.com) or a domain (company.com).")
    key = value if kind == "email" else "@" + value
    existing = await _exact_block(ts, key)
    if existing is not None:
        return existing, False
    return await suppress(ts, email=key, reason="manual",
                          created_by_user_id=created_by_user_id), True


def is_domain_block(block: DoNotContact) -> bool:
    return (block.email or "").startswith("@")


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
