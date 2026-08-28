# nexus/billing/usage.py
"""Usage recording: the append-only, idempotent write path for the metering engine.

Two hard rules (docs/billing/01-Billing-Architecture.md §6):
  1. Recording usage must NEVER raise into product code. A metering outage degrades telemetry,
     never the feature.
  2. The same idempotency key must never bill twice.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingUsageEvent

logger = logging.getLogger("nexus.billing.usage")


async def record_usage(
    ts: TenantSession,
    *,
    capability_id: str,
    quantity: float = 1,
    unit: str = "action",
    user_id: str | None = None,
    source: str = "api",
    idempotency_key: str | None = None,
    attrs: dict | None = None,
    unit_cost_usd: float | None = None,
) -> bool:
    """Append one usage event. Returns True if a row was written, False if it was a no-op.

    A no-op means either a duplicate idempotency key (already billed) or a swallowed error —
    both are safe outcomes for the caller, which never needs to branch on the result.
    """
    if not capability_id:
        logger.warning("record_usage called without capability_id; ignoring")
        return False
    try:
        key = idempotency_key or f"auto:{uuid.uuid4().hex}"
        if idempotency_key and await _already_recorded(ts, key):
            return False
        event = BillingUsageEvent(
            capability_id=capability_id, quantity=quantity, unit=unit, user_id=user_id,
            source=source, idempotency_key=key, attrs=attrs or {},
            unit_cost_usd=unit_cost_usd, occurred_at=utcnow(),
        )
        # SAVEPOINT, not a bare flush. The check above and this INSERT are not atomic, so two
        # callers holding one idempotency key both pass the check and the loser violates
        # `uq_usage_idempotency`. Catching that at the outer `except` is not enough: on Postgres
        # the transaction is ALREADY aborted, so swallowing the error and returning leaves the
        # caller's next statement raising PendingRollbackError — this module breaking the product
        # in exactly the case its "never breaks the product" guarantee was written for. Rolling
        # back to a savepoint discards only the failed INSERT.
        try:
            async with ts.session.begin_nested():
                ts.session.add(event)
                await ts.session.flush()
        except IntegrityError:
            # Someone else billed this key between the check and the write. That is the correct
            # outcome, not an error: exactly one row exists and the caller was not charged twice.
            logger.debug("usage key %s already applied by a concurrent writer", key)
            return False
        return True
    except Exception:  # metering must never break the product
        logger.warning("record_usage failed for %s", capability_id, exc_info=True)
        return False


async def _already_recorded_impl(ts: TenantSession, key: str) -> bool:
    """Has this idempotency key already been billed for this tenant?"""
    existing = (
        await ts.session.scalars(
            ts.select(BillingUsageEvent, BillingUsageEvent.idempotency_key == key).limit(1)
        )
    ).first()
    return existing is not None


# Indirected so a test can reproduce the interleaving where this check misses a committed row.
_already_recorded = _already_recorded_impl
