# nexus/billing/entitlements.py
"""The entitlement engine: resolve policy, decide, and meter — the ONE billing seam.

Resolution order (docs/billing/02-Entitlement-Engine.md §2):
    plan class 'unlimited'  ->  plan entitlement  ->  catalog default  ->  unknown (allow)

Everything about this module is biased toward NOT breaking the product: unknown capabilities,
missing subscriptions, and internal errors all resolve to "allow".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
)

logger = logging.getLogger("nexus.billing.entitlements")

# Plan classes that are never limited: they exist to observe cost, not to gate features.
_UNLIMITED_CLASSES = {"unlimited", "internal", "partner"}
# Subscription states in which entitlements still apply normally.
_LIVE_STATUSES = {"trialing", "active", "past_due"}


@dataclass(slots=True)
class ResolvedEntitlement:
    """The effective policy for one tenant x capability, plus where it came from (for admin
    debugging and the 402 payload)."""

    capability_id: str
    mode: str                       # shadow|enabled|metered|unlimited|disabled|enterprise
    quota: int | None = None
    soft_limit_pct: int = 80
    hard_limit: int | None = None
    overage_price_credits: int | None = None
    cooldown_s: int | None = None
    burst_limit: int | None = None
    reset_policy: str = "monthly_anniversary"
    depends_on: tuple[str, ...] = ()
    unit: str = "action"
    plan_id: str | None = None
    source: str = "catalog"         # plan_class | plan | catalog | unknown


async def _active_subscription(ts: TenantSession) -> BillingSubscription | None:
    rows = await ts.list(BillingSubscription, limit=5)
    live = [s for s in rows if s.status in _LIVE_STATUSES]
    return live[0] if live else (rows[0] if rows else None)


async def resolve_entitlement(ts: TenantSession, capability_id: str) -> ResolvedEntitlement:
    """Compute the effective entitlement. Never raises."""
    try:
        cap = await ts.session.get(BillingCapability, capability_id)
        if cap is None:
            # Unregistered capability: allow and record. This is what makes shipping the engine
            # incapable of breaking a feature nobody remembered to catalog.
            return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")

        base = ResolvedEntitlement(
            capability_id=capability_id,
            mode=cap.default_mode,
            unit=cap.unit,
            depends_on=tuple(cap.depends_on or ()),
            source="catalog",
        )

        sub = await _active_subscription(ts)
        if sub is None:
            return base
        base.plan_id = sub.plan_id

        plan = await ts.session.get(BillingPlan, sub.plan_id)
        if plan is not None and plan.plan_class in _UNLIMITED_CLASSES:
            base.mode = "unlimited"
            base.quota = None
            base.source = "plan_class"
            return base

        ent = (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id,
                    BillingPlanEntitlement.capability_id == capability_id,
                )
            )
        ).first()
        if ent is None:
            return base

        base.mode = ent.mode
        base.quota = ent.quota
        base.soft_limit_pct = ent.soft_limit_pct
        base.hard_limit = ent.hard_limit
        base.overage_price_credits = ent.overage_price_credits
        base.cooldown_s = ent.cooldown_s
        base.burst_limit = ent.burst_limit
        base.reset_policy = ent.reset_policy
        base.source = "plan"
        return base
    except Exception:  # resolution failure must degrade to allow, never to a 500
        logger.warning("entitlement resolution failed for %s", capability_id, exc_info=True)
        return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")


# ---- the seam ------------------------------------------------------------------------------
@dataclass(slots=True)
class MeterResult:
    """Outcome of one seam call. Callers only ever need ``allowed``."""

    allowed: bool
    recorded: bool = False
    reason: str | None = None        # quota_exhausted | disabled | dependency | throttled
    would_block: bool = False        # True when shadow mode suppressed a real block
    used: float = 0
    quota: int | None = None
    entitlement: "ResolvedEntitlement | None" = None

    def raise_if_blocked(self) -> None:
        """Convenience for routers: turn a block into the typed HTTP-mappable exception."""
        if self.allowed:
            return
        from nexus.billing.errors import QuotaExceeded

        ent = self.entitlement
        raise QuotaExceeded(
            ent.capability_id if ent else "unknown",
            reason=self.reason or "quota_exhausted",
            used=self.used,
            quota=self.quota,
            plan_id=ent.plan_id if ent else None,
        )


async def current_usage(ts: TenantSession, capability_id: str) -> float:
    """Authoritative usage for the current billing period.

    Reads the ``period`` rollup (O(1)) and adds any events recorded after the rollup watermark,
    so the number is always exact even between rollup runs. Postgres — never the cache — is the
    source of truth for hard limits: a cache wipe must not hand out free quota
    (docs/billing/02-Entitlement-Engine.md §4).
    """
    from sqlalchemy import func

    from nexus.billing.rollups import period_key
    from nexus.core.db import ensure_aware, utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    now = utcnow()
    key = period_key(now, "period")
    rollup = (
        await ts.session.scalars(
            ts.select(
                BillingUsageRollup,
                BillingUsageRollup.capability_id == capability_id,
                BillingUsageRollup.period_kind == "period",
                BillingUsageRollup.period_key == key,
            ).limit(1)
        )
    ).first()

    total = float(rollup.quantity) if rollup is not None else 0.0
    # Events newer than the rollup's last write are not yet reflected in it.
    watermark = ensure_aware(rollup.updated_at) if rollup is not None else None
    stmt = ts.select(BillingUsageEvent).where(
        BillingUsageEvent.capability_id == capability_id
    )
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    conds = [BillingUsageEvent.occurred_at >= period_start]
    if watermark is not None:
        conds.append(BillingUsageEvent.created_at > watermark)
    unrolled = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
            *conds,
        )
    )
    return total + float(unrolled or 0)


async def check_and_meter(
    ts: TenantSession,
    *,
    capability_id: str,
    quantity: float = 1,
    user_id: str | None = None,
    source: str = "api",
    idempotency_key: str | None = None,
    attrs: dict | None = None,
) -> MeterResult:
    """THE billing seam. Resolve entitlement, decide, record usage.

    Application code calls exactly this and never mentions a plan. Behavior is governed by
    ``NEXUS_BILLING_ENFORCEMENT``:
      off    -> pure passthrough (no evaluation, no recording)
      shadow -> evaluate + record, but ALWAYS allow (``would_block`` reports what would happen)
      on     -> evaluate + record + enforce

    Never raises: any internal failure degrades to allow (docs/billing/01 §6).
    """
    from nexus.billing.usage import record_usage
    from nexus.core.config import get_settings

    mode = get_settings().billing_enforcement
    if mode == "off":
        return MeterResult(allowed=True, recorded=False)

    try:
        ent = await resolve_entitlement(ts, capability_id)

        blocked_reason: str | None = None
        used = 0.0
        if ent.mode == "disabled":
            blocked_reason = "disabled"
        elif ent.mode in ("shadow", "enabled", "unlimited"):
            blocked_reason = None            # never quota-limited
        elif ent.quota is not None:
            used = await current_usage(ts, capability_id)
            limit = ent.hard_limit if ent.hard_limit is not None else ent.quota
            # Overage pricing means "keep going and charge for it", not "stop".
            if used + quantity > limit and ent.overage_price_credits is None:
                blocked_reason = "quota_exhausted"

        enforced = mode == "on"
        allowed = True if not enforced else blocked_reason is None

        recorded = False
        if allowed:
            recorded = await record_usage(
                ts, capability_id=capability_id, quantity=quantity, unit=ent.unit,
                user_id=user_id, source=source, idempotency_key=idempotency_key, attrs=attrs,
            )
        return MeterResult(
            allowed=allowed,
            recorded=recorded,
            reason=blocked_reason if not allowed else None,
            would_block=blocked_reason is not None,
            used=used,
            quota=ent.quota,
            entitlement=ent,
        )
    except Exception:  # the seam must never break the product
        logger.warning("check_and_meter failed for %s", capability_id, exc_info=True)
        return MeterResult(allowed=True, recorded=False)
