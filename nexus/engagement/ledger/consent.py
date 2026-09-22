"""Whether a workspace contributes to the training & insights ledger (D24, spec §18.6).

**Append-only decisions; the newest is in force.** A workspace with no row has never been asked —
that is every workspace created before the ledger — and reads ``pending``: nothing is collected until
an owner or admin answers the one-time prompt. New workspaces record their sign-up choice in the same
transaction that creates them, so they are never ``pending``.

**Read once per transaction.** ``status`` caches the answer on the SQLAlchemy session, so an action
that emits ten events costs one query. A decision recorded through ``record`` clears that cache, and
every other request or job opens its own session, so switching off takes effect for the very next
transaction anywhere.
"""
from __future__ import annotations

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.ledger import CONSENT_SOURCES, TrainingConsent

#: Bumped when what is collected, or how it is used, changes. Stored on every decision and on every
#: training example, so data collected under older terms can be told apart.
TERMS_VERSION = "2026-09-17"
_CACHE_KEY = "ledger_consent_status"


async def latest(ts: TenantSession) -> TrainingConsent | None:
    stmt = (ts.select(TrainingConsent)
            .order_by(TrainingConsent.decided_at.desc(), TrainingConsent.created_at.desc())
            .limit(1))
    return (await ts.session.scalars(stmt)).first()


async def status(ts: TenantSession) -> str:
    """``on``, ``off`` or ``pending``. Never raises: an unreadable answer is ``pending``."""
    cached = ts.session.info.get(_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        row = await latest(ts)
    except Exception:
        return "pending"
    value = row.status if row is not None else "pending"
    ts.session.info[_CACHE_KEY] = value
    return value


async def record(ts: TenantSession, *, status_value: str, source: str,
                 user_id: str | None) -> TrainingConsent:
    if status_value not in ("on", "off"):
        raise ValueError("choose on or off")
    if source not in CONSENT_SOURCES:
        raise ValueError(f"unknown consent source {source!r}")
    row = TrainingConsent(status=status_value, source=source, terms_version=TERMS_VERSION,
                          decided_by_user_id=user_id, decided_at=utcnow())
    ts.add(row)
    await ts.flush()
    ts.session.info.pop(_CACHE_KEY, None)
    return row


def add_signup_decision(session, *, tenant_id: str, user_id: str, opted_in: bool) -> None:
    """Stage the sign-up choice on a raw session inside the tenant-creating transaction.

    Sign-up has no ``TenantSession`` yet, so the row names its tenant explicitly; the flush guard
    accepts a row that already carries one."""
    session.add(TrainingConsent(
        tenant_id=tenant_id, status="on" if opted_in else "off", source="signup",
        terms_version=TERMS_VERSION, decided_by_user_id=user_id, decided_at=utcnow(),
    ))

