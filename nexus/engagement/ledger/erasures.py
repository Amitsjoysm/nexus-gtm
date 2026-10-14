"""Who has been erased from the ledger, and whether an event names them (spec §18.6, 2026-10-14).

Erasure used to delete a person from the three stores and stop there, which left three ways back:

* **The outbox.** It holds every event as written, raw email text included, until the archive
  takes it and for seven days after. Unshipped rows (an unconfigured or failing archive) shipped the
  person straight back into the archive after the erasure ran.
* **Anyone but the contact.** Archive rows were findable only by the contact an email was addressed
  to. A colleague on the Cc line, a referral, an address in a quoted thread or a signature could not
  be erased at all.
* **The next event.** Nothing remembered the request, so the next email to that person was
  collected again.

So an erasure is now a row here, and **an event that names an erased person is never shipped and
never built**: the shipper deletes it from the outbox instead, the builder skips it. "Names" is
:func:`everyone`: the contact, the SDR, and every address written anywhere in the event.

The row holds no address. ``erasure_key`` is an HMAC of the address under the deployment's
``secret_key``, not the ledger's pseudonymisation secret, so a request is honoured even before the
ledger is configured, which is exactly when the outbox is filling up with nowhere to ship. Rotating
``secret_key`` would stop old rows matching; it already invalidates every session and sealed value.
"""
from __future__ import annotations

import hashlib
import hmac

from nexus.engagement.ledger.scrub import EMAIL_RE


def _normal(email: str) -> str:
    return " ".join((email or "").strip().lower().split())


def erasure_key(email: str) -> str:
    from nexus.core.config import get_settings

    secret = f"nexus-ledger-erasure:{get_settings().secret_key}".encode()
    return hmac.new(secret, _normal(email).encode(), hashlib.sha256).hexdigest()


def _strings(value, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)


def everyone(envelope: dict, resolved: dict) -> list[str]:
    """Every address an event names: the contact it was about, the SDR who sent it, and any
    address written in it (a Cc line, a referral, a quoted thread, a signature).

    Each string is searched on its own, never a JSON dump of the whole: serialising turns a line
    break into ``\\n``, and ``n`` is a valid first character of an address, so "Hi\\njane@acme.io"
    would be read as ``njane@acme.io``."""
    found = {_normal(e) for e in (resolved.get("person_emails") or []) if e}
    for e in (resolved.get("contact_email"), resolved.get("sdr_email")):
        if e:
            found.add(_normal(e))
    for text in _strings(envelope):
        for match in EMAIL_RE.findall(text):
            found.add(_normal(match.strip(".'")))
    found.discard("")
    return sorted(found)


def names_anyone(blocked: set[str], envelope: dict, resolved: dict) -> bool:
    """Does this event name anyone in ``blocked`` (a set of erasure keys)?"""
    if not blocked:
        return False
    return any(erasure_key(email) in blocked for email in everyone(envelope, resolved))


async def blocked_keys() -> set[str]:
    """Every erased person's key. Raises when it cannot be read: shipping or building without
    knowing who was erased is how an erased person comes back, so the caller must not go on."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerErasure

    async with get_platform_sessionmaker()() as session:
        return set((await session.execute(select(LedgerErasure.erasure_key))).scalars().all())


async def record_request(email: str, *, person_key: str, user_id: str | None):
    """Remember the request and return its row. Asking again for the same person runs the
    erasure again on the same row, keeping what earlier runs found (their event ids)."""
    return await ensure(erasure_key(email), person_key=person_key, user_id=user_id)


async def ensure(key: str, *, person_key: str, user_id: str | None = None):
    """The erasure row for ``key``, created or re-opened. Used by the request above and by the
    job when the request came from somewhere holding a transaction (``people.store``)."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerErasure

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(
            select(LedgerErasure).where(LedgerErasure.erasure_key == key))).scalars().first()
        if row is None:
            row = LedgerErasure(erasure_key=key, report={})
            session.add(row)
        row.person_key = person_key or row.person_key or ""
        row.requested_by_user_id = user_id
        row.requested_at = utcnow()
        row.finished_at = None
        await session.commit()
        await session.refresh(row)
        return row


async def get(erasure_id: str):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerErasure

    async with get_platform_sessionmaker()() as session:
        return await session.get(LedgerErasure, erasure_id)


async def finish(erasure_id: str, report: dict) -> None:
    """Store what this run deleted and found left. Finished only when nothing failed: a store that
    errored leaves the request open, and the job is retried."""
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerErasure

    async with get_platform_sessionmaker()() as session:
        row = await session.get(LedgerErasure, erasure_id)
        if row is None:
            return
        row.report = report
        row.finished_at = None if report.get("errors") else utcnow()
        await session.commit()


async def recent(limit: int = 10) -> list:
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerErasure

    async with get_platform_sessionmaker()() as session:
        return list((await session.execute(
            select(LedgerErasure).order_by(LedgerErasure.requested_at.desc()).limit(limit)
        )).scalars().all())
