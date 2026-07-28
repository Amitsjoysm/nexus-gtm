# nexus/billing/subscriptions.py
"""Subscription lifecycle: assignment, plan change, period roll.

Every tenant must hold exactly one subscription, because the entitlement chain resolves
plan -> capability. A tenant without one falls through to catalog defaults, which is fine in
shadow mode and dangerous the moment enforcement is armed — so ``backfill_subscriptions`` runs
on every boot and is the reason arming enforcement is safe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from nexus.core.db import get_sessionmaker, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingPlan, BillingSubscription

logger = logging.getLogger("nexus.billing.subscriptions")

ACTIVE_STATUSES = ("trialing", "active", "past_due")


def next_period_end(start: datetime, interval: str = "month") -> datetime:
    """End of the billing period beginning at ``start``.

    Calendar-month arithmetic, not "+30 days": customers reconcile invoices against calendar
    months, and a drifting anniversary makes every support conversation harder.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if interval == "year":
        return start.replace(year=start.year + 1)
    year, month = start.year, start.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    # Clamp the day so the 31st of a month does not overflow a shorter one.
    day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return start.replace(year=year, month=month, day=day)


async def ensure_subscription(
    ts: TenantSession, *, plan_id: str, status: str = "active", grandfathered: bool = False,
) -> BillingSubscription | None:
    """Give this tenant a subscription if it has none. Returns the new row, or None if one
    already existed — never modifies an existing subscription."""
    existing = await ts.list(BillingSubscription, limit=1)
    if existing:
        return None
    now = utcnow()
    sub = BillingSubscription(
        plan_id=plan_id, status=status, grandfathered=grandfathered,
        current_period_start=now, current_period_end=next_period_end(now),
    )
    ts.add(sub)
    await ts.flush()
    return sub


async def backfill_subscriptions() -> dict:
    """Put every tenant that has no subscription onto the legacy unlimited plan.

    Idempotent and additive: a tenant that has since upgraded is left completely alone, so a
    redeploy can never downgrade a paying customer.
    """
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, LEGACY_PLAN_ID) is None:
            logger.warning("legacy plan missing; skipping subscription backfill")
            return {"created": 0, "skipped": "no_legacy_plan"}
        tenant_ids = list((await session.scalars(select(Tenant.id))).all())

    created = 0
    for tid in tenant_ids:
        try:
            async with get_sessionmaker()() as session:
                ts = TenantSession(session, tid)
                sub = await ensure_subscription(
                    ts, plan_id=LEGACY_PLAN_ID, status="active", grandfathered=True,
                )
                if sub is not None:
                    created += 1
                await session.commit()
        except Exception:  # one bad tenant must not stop the backfill
            logger.warning("subscription backfill failed for %s", tid, exc_info=True)
    if created:
        logger.info("subscription backfill: %d tenant(s) placed on %s", created, LEGACY_PLAN_ID)
    return {"created": created}
