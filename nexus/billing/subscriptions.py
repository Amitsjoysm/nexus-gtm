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
from nexus.core.tenancy import TenantSession, apply_rls
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
                # Bind the tenant GUC before writing. The API runs as the least-privilege
                # `nexus_app` role with RLS enforced, so an unbound INSERT is rejected by
                # Postgres ("new row violates row-level security policy"). SQLite has no RLS,
                # which is why this only ever surfaces against the real database.
                await apply_rls(session, tid)
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


async def _active(
    ts: TenantSession, *, statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> BillingSubscription | None:
    subs = await ts.session.scalars(
        ts.select(BillingSubscription, BillingSubscription.status.in_(statuses))
        .order_by(BillingSubscription.created_at.desc(), BillingSubscription.id.desc())
    )
    return subs.first()


async def change_plan(ts: TenantSession, plan_id: str, *, actor: str = "system"):
    """Move a tenant to another plan, in place.

    Switching the existing row rather than opening a second one keeps "one subscription per
    tenant" true at all times — two active rows would make rating ambiguous.
    """
    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_id}")
    sub = await _active(ts)
    if sub is None:
        return await ensure_subscription(ts, plan_id=plan_id)

    previous = sub.plan_id
    adjustments = await _record_proration(ts, sub, old_plan_id=previous, new_plan=plan, actor=actor)
    sub.plan_id = plan_id
    sub.status = "active"
    sub.currency = plan.currency
    sub.interval = plan.interval
    # Grandfathered terms are frozen legacy pricing; taking a new plan means taking its terms.
    sub.grandfathered = False
    sub.cancel_at_period_end = False
    sub.meta = {
        **(sub.meta or {}),
        "previous_plan_id": previous,
        "changed_by": actor,
        "changed_at": utcnow().isoformat(),
        "last_proration_cents": sum(a.amount_cents for a in adjustments),
    }
    await ts.flush()
    return sub


async def preview_proration(ts: TenantSession, *, plan_id: str, at: datetime | None = None):
    """What a plan change would cost, without writing anything.

    An admin changing a customer's plan is committing real money on that customer's behalf; being
    able to see the number first is the difference between a decision and a surprise.
    """
    from nexus.billing.lifecycle import Proration, prorate

    plan = await ts.session.get(BillingPlan, plan_id)
    sub = await _active(ts)
    if plan is None or sub is None:
        return Proration()
    old_plan = await ts.session.get(BillingPlan, sub.plan_id)
    if not _proratable(sub, old_plan_id=sub.plan_id, new_plan_id=plan_id):
        return Proration()
    return prorate(
        old_monthly_cents=(old_plan.base_price_cents if old_plan else 0),
        new_monthly_cents=plan.base_price_cents,
        period_start=_aware(sub.current_period_start),
        period_end=_aware(sub.current_period_end),
        at=at or utcnow(),
    )


def _aware(value: datetime | None) -> datetime:
    """SQLite hands back naive datetimes; proration arithmetic compares them against ``utcnow``."""
    if value is None:
        return utcnow()
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _proratable(sub: BillingSubscription, *, old_plan_id: str, new_plan_id: str) -> bool:
    """Whether WE should prorate this change.

    Three deliberate refusals:

    * **Same plan** — a no-op write must not produce two lines that cancel out.
    * **No billing period** — nothing has been paid for, so there is nothing to day-weight.
    * **Provider-owned subscription** — Stripe prorates its own changes. Adding our lines on top
      bills the difference twice, once on their invoice and once on ours, and the customer would
      be right to dispute it. This is why the Stripe webhook writes ``sub.plan_id`` directly
      rather than calling ``change_plan``.
    """
    if old_plan_id == new_plan_id:
        return False
    if sub.current_period_start is None or sub.current_period_end is None:
        return False
    if sub.psp_subscription_id:
        return False
    return True


async def _record_proration(
    ts: TenantSession, sub: BillingSubscription, *, old_plan_id: str, new_plan, actor: str,
) -> list:
    """Write the credit/charge pair for a mid-cycle change. Returns the rows written."""
    from nexus.billing.lifecycle import prorate
    from nexus.billing.rollups import period_key
    from nexus.models.billing import BillingProrationAdjustment

    if not _proratable(sub, old_plan_id=old_plan_id, new_plan_id=new_plan.id):
        return []

    old_plan = await ts.session.get(BillingPlan, old_plan_id)
    now = utcnow()
    p = prorate(
        old_monthly_cents=(old_plan.base_price_cents if old_plan else 0),
        new_monthly_cents=new_plan.base_price_cents,
        period_start=_aware(sub.current_period_start),
        period_end=_aware(sub.current_period_end),
        at=now,
    )
    if p.credit_cents == 0 and p.charge_cents == 0:
        return []

    pk = period_key(now, "period")
    common = dict(
        period_key=pk, from_plan_id=old_plan_id, to_plan_id=new_plan.id,
        days_remaining=p.days_remaining, days_in_period=p.days_in_period,
        effective_at=now, actor=actor,
    )
    rows = [
        # Signed negative: summing the lines gives the net with no special-casing by kind.
        BillingProrationAdjustment(
            kind="proration_credit", amount_cents=-p.credit_cents,
            description=(
                f"Unused {old_plan.name if old_plan else old_plan_id} "
                f"({p.days_remaining} of {p.days_in_period} days)"
            ),
            **common,
        ),
        BillingProrationAdjustment(
            kind="proration_charge", amount_cents=p.charge_cents,
            description=(
                f"{new_plan.name} for the rest of the period "
                f"({p.days_remaining} of {p.days_in_period} days)"
            ),
            **common,
        ),
    ]
    for row in rows:
        ts.add(row)
    await ts.flush()
    logger.info(
        "prorated %s -> %s for tenant %s: credit %d, charge %d",
        old_plan_id, new_plan.id, ts.tenant_id, -p.credit_cents, p.charge_cents,
    )
    return rows


async def pause_subscription(ts: TenantSession, *, actor: str = "system") -> BillingSubscription:
    """Pause billing and access. Raises ``BillingError`` when the status forbids it.

    Records ``paused_at`` so ``resume_subscription`` can give the paused days back — without that
    the customer pays for thirty days and receives sixteen, which is the same overcharge proration
    exists to prevent.
    """
    from nexus.billing.errors import BillingError
    from nexus.billing.lifecycle import can_pause

    sub = await _active(ts)
    if sub is None:
        raise BillingError("no subscription to pause")
    ok, why = can_pause(sub.status)
    if not ok:
        raise BillingError(why)

    sub.status = "suspended"
    sub.meta = {**(sub.meta or {}), "paused_at": utcnow().isoformat(), "paused_by": actor}
    await ts.flush()
    return sub


async def resume_subscription(ts: TenantSession, *, actor: str = "system") -> BillingSubscription:
    """Un-pause and push the period end out by however long the pause lasted."""
    from nexus.billing.errors import BillingError
    from nexus.billing.lifecycle import can_resume, paused_extension

    sub = await _active(ts, statuses=("suspended", *ACTIVE_STATUSES))
    if sub is None:
        raise BillingError("no subscription to resume")
    ok, why = can_resume(sub.status)
    if not ok:
        raise BillingError(why)

    now = utcnow()
    meta = dict(sub.meta or {})
    paused_raw = meta.pop("paused_at", None)
    if paused_raw and sub.current_period_end is not None:
        try:
            paused_at = datetime.fromisoformat(str(paused_raw))
        except ValueError:
            paused_at = now
        extension = paused_extension(_aware(paused_at), now)
        sub.current_period_end = _aware(sub.current_period_end) + extension
        meta["last_pause_days"] = extension.days

    sub.status = "active"
    meta["resumed_at"] = now.isoformat()
    meta["resumed_by"] = actor
    sub.meta = meta
    await ts.flush()
    return sub


async def cancel_subscription(ts: TenantSession, *, at_period_end: bool = True):
    """Cancel. At period end by default — the customer paid through it."""
    sub = await _active(ts)
    if sub is None:
        return None
    if at_period_end:
        sub.cancel_at_period_end = True
    else:
        sub.status = "canceled"
        sub.cancel_at_period_end = False
    await ts.flush()
    return sub


async def roll_period(ts: TenantSession) -> bool:
    """Close the current period if it has ended. Returns True if a roll happened.

    Order matters: rate and finalize BEFORE advancing the window, so the invoice describes the
    period that just closed rather than the one starting.
    """
    from nexus.billing.credits import grant_credits
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups

    sub = await _active(ts)
    if sub is None or sub.current_period_end is None:
        return False
    now = utcnow()
    end = sub.current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end > now:
        return False

    closing_key = period_key(sub.current_period_start or end, "period")

    # Full-window rebuild: the heartbeat sweep only covers the current period, so stragglers
    # written just after the boundary are folded in here, at the one moment it matters.
    await rebuild_rollups(ts)
    invoice = await rate_period(ts, period_key=closing_key)
    await finalize_invoice(ts, invoice.id)

    if sub.cancel_at_period_end:
        sub.status = "canceled"
        await ts.flush()
        return True

    sub.current_period_start = now
    sub.current_period_end = next_period_end(now, sub.interval)
    await ts.flush()

    plan = await ts.session.get(BillingPlan, sub.plan_id)
    if plan is not None and plan.included_credits:
        new_key = period_key(now, "period")
        await grant_credits(
            ts, plan.included_credits, kind="grant",
            reason=f"{plan.name} included credits",
            # Keyed by period, so a retried job grants once and only once.
            idempotency_key=f"plan_grant:{new_key}", period_key=new_key,
        )
    return True
