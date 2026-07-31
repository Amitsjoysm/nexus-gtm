# nexus/billing/credits.py
"""Credit ledger: append-only movements, balance = SUM(delta).

Never a mutable counter. An append-only ledger is the only shape that stays correct under
concurrency and remains auditable when a customer disputes a charge
(docs/billing/04-Pricing-Engine.md §1).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingCreditLedger

logger = logging.getLogger("nexus.billing.credits")


async def balance(ts: TenantSession) -> float:
    """Current credit balance for the tenant."""
    total = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingCreditLedger.delta), 0)).where(
            BillingCreditLedger.tenant_id == ts.tenant_id
        )
    )
    return float(total or 0)


async def _append(
    ts: TenantSession, delta: float, *, kind: str, reason: str, idempotency_key: str,
    capability_id: str | None = None, period_key: str | None = None,
    expires_at: datetime | None = None, actor: str = "system",
) -> bool:
    """Append one movement unless the idempotency key was already used."""
    dup = await ts.first(
        BillingCreditLedger, BillingCreditLedger.idempotency_key == idempotency_key
    )
    if dup is not None:
        return False
    ts.add(
        BillingCreditLedger(
            delta=delta, kind=kind, reason=reason, capability_id=capability_id,
            period_key=period_key, expires_at=expires_at,
            idempotency_key=idempotency_key, actor=actor,
        )
    )
    await ts.flush()
    return True


async def grant_credits(
    ts: TenantSession, amount: float, *, kind: str = "grant", reason: str = "",
    idempotency_key: str, expires_at: datetime | None = None, actor: str = "system",
    period_key: str | None = None,
) -> bool:
    """Add credits (monthly grant, purchased pack, promo, manual adjustment)."""
    if amount <= 0:
        return False
    return await _append(
        ts, float(amount), kind=kind, reason=reason, idempotency_key=idempotency_key,
        expires_at=expires_at, actor=actor, period_key=period_key,
    )


async def burn_credits(
    ts: TenantSession, amount: float, *, reason: str = "", idempotency_key: str,
    capability_id: str | None = None, period_key: str | None = None,
    allow_negative: bool = False, actor: str = "system",
) -> bool:
    """Spend credits. Refuses to overdraw unless ``allow_negative`` (overage billing).

    Returns False when refused or when the key was already applied — the caller never needs to
    distinguish, because both mean "no new charge was made".
    """
    if amount <= 0:
        return False
    if not allow_negative and await balance(ts) < amount:
        return False
    applied = await _append(
        ts, -float(amount), kind="burn", reason=reason, idempotency_key=idempotency_key,
        capability_id=capability_id, period_key=period_key, actor=actor,
    )
    if applied:
        # Only a burn that actually landed. Counting refusals and replayed keys here would make
        # the burn rate climb while nothing was being charged.
        from nexus.core import metrics

        metrics.record_credit_burn(capability_id or "unattributed", amount)
    return applied


async def history(ts: TenantSession, *, limit: int = 100) -> list[BillingCreditLedger]:
    """Most recent movements first — powers the customer's credit history view."""
    rows = await ts.session.scalars(
        ts.select(BillingCreditLedger)
        .order_by(BillingCreditLedger.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())
