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
        if idempotency_key:
            existing = (
                await ts.session.scalars(
                    ts.select(
                        BillingUsageEvent, BillingUsageEvent.idempotency_key == key
                    ).limit(1)
                )
            ).first()
            if existing is not None:
                return False
        ts.add(
            BillingUsageEvent(
                capability_id=capability_id, quantity=quantity, unit=unit, user_id=user_id,
                source=source, idempotency_key=key, attrs=attrs or {},
                unit_cost_usd=unit_cost_usd, occurred_at=utcnow(),
            )
        )
        await ts.flush()
        return True
    except Exception:  # metering must never break the product
        logger.warning("record_usage failed for %s", capability_id, exc_info=True)
        return False
