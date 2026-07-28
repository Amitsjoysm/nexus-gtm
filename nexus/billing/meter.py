# nexus/billing/meter.py
"""``metered()`` — the one call application code makes to bill an action.

Wraps the M2 gate so a caller writes::

    async with metered(ts, "ai.email_draft", user_id=principal.user_id):
        draft = await write_the_draft()

and gets: the quota decision, the usage row, the measured COGS stamped onto that row, and a
compensating row if the body raises. Application code still never mentions a plan or a price.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from nexus.billing.context import cost_scope
from nexus.billing.entitlements import MeterResult, check_and_meter
from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.billing.meter")


@asynccontextmanager
async def metered(
    ts: TenantSession,
    capability_id: str,
    *,
    quantity: float = 1,
    user_id: str | None = None,
    source: str = "api",
    attrs: dict | None = None,
    idempotency_key: str | None = None,
) -> AsyncIterator[MeterResult]:
    """Gate, record, measure cost, and refund on failure.

    Raises ``QuotaExceeded`` (HTTP 402) only when enforcement is ``on`` AND the plan says no.
    """
    key = idempotency_key or f"{capability_id}:{uuid4().hex}"
    result = await check_and_meter(
        ts, capability_id=capability_id, quantity=quantity, user_id=user_id,
        source=source, idempotency_key=key, attrs=attrs,
    )
    # M2 already owns the block -> 402 translation; reuse it so the payload can never drift.
    result.raise_if_blocked()

    try:
        with cost_scope() as cost:
            yield result
    except Exception:
        # The action failed after we charged for it. Append a compensating row rather than
        # deleting: the event stream is the audit trail, and a disputed charge has to be
        # explainable, not merely absent.
        if result.recorded:
            await _refund(ts, capability_id, quantity, key, user_id, source)
        raise

    if result.recorded and cost.usd > 0:
        await _stamp_cost(ts, key, cost.usd / max(float(quantity), 1.0))


async def _refund(
    ts: TenantSession, capability_id: str, quantity: float, key: str,
    user_id: str | None, source: str,
) -> None:
    from nexus.billing.usage import record_usage

    try:
        await record_usage(
            ts, capability_id=capability_id, quantity=-float(quantity), user_id=user_id,
            source=source, idempotency_key=f"{key}:refund",
            attrs={"refund_of": key, "reason": "action_failed"},
        )
    except Exception:  # never let bookkeeping mask the caller's real exception
        logger.warning("refund failed for %s", capability_id, exc_info=True)


async def _stamp_cost(ts: TenantSession, key: str, unit_cost_usd: float) -> None:
    from nexus.models.billing import BillingUsageEvent

    try:
        ev = await ts.first(BillingUsageEvent, BillingUsageEvent.idempotency_key == key)
        if ev is not None:
            ev.unit_cost_usd = unit_cost_usd
            await ts.flush()
    except Exception:
        logger.warning("cost stamp failed for %s", key, exc_info=True)
