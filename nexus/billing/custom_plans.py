# nexus/billing/custom_plans.py
"""Bespoke per-customer pricing, built in Admin and published to the payment provider.

Enterprise sales does not fit a fixed price list: a customer negotiates a price, a credit
allowance, and specific quotas. This creates a real plan row for that customer — cloned from a
base plan so nothing has to be specified from scratch — applies the negotiated overrides,
publishes it as a product + price at the PSP, and assigns it.

Why a plan row rather than per-tenant overrides on the subscription: the entitlement engine
already resolves plan -> capability, rating already reads plan prices, and the admin UI already
lists plans. A custom plan is therefore free of new code paths, and a bespoke deal behaves
exactly like a standard one everywhere downstream (docs/billing/07-Enterprise-Licensing.md).
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.billing import BillingPlan, BillingPlanEntitlement

logger = logging.getLogger("nexus.billing.custom_plans")

CUSTOM_PREFIX = "custom-"
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


class CustomPlanError(ValueError):
    """The requested custom plan cannot be built."""


def custom_plan_id(tenant_slug: str) -> str:
    slug = _SLUG_RE.sub("-", (tenant_slug or "").lower()).strip("-") or "tenant"
    return f"{CUSTOM_PREFIX}{slug}"[:60]


async def create_custom_plan(
    session: AsyncSession,
    *,
    plan_id: str,
    name: str,
    base_plan_id: str,
    base_price_cents: int,
    included_credits: int,
    currency: str = "USD",
    interval: str = "month",
    max_seats: int | None = None,
    entitlement_overrides: dict[str, dict] | None = None,
    publish_to_provider: bool = True,
) -> dict:
    """Create or update a per-customer plan and publish it to the payment provider.

    ``entitlement_overrides`` maps capability_id -> field overrides, applied on top of the
    entitlements cloned from ``base_plan_id``. A capability the base plan never had is added.
    """
    base = await session.get(BillingPlan, base_plan_id)
    if base is None:
        raise CustomPlanError(f"unknown base plan '{base_plan_id}'")
    if base_price_cents < 0 or included_credits < 0:
        raise CustomPlanError("price and credits cannot be negative")

    plan = await session.get(BillingPlan, plan_id)
    created = plan is None
    if plan is None:
        plan = BillingPlan(id=plan_id)
        session.add(plan)

    plan.name = name or f"{base.name} (custom)"
    plan.description = f"Custom plan derived from {base.name}"
    # plan_class "custom" keeps it out of the public price list while behaving like a standard
    # paid plan for the entitlement engine and rating.
    plan.plan_class = "custom"
    plan.status = "active"
    plan.base_price_cents = int(base_price_cents)
    plan.seat_price_cents = base.seat_price_cents
    plan.currency = currency
    plan.interval = interval
    plan.included_credits = int(included_credits)
    plan.max_seats = max_seats if max_seats is not None else base.max_seats
    plan.trial_days = 0
    plan.sort_order = 900          # bespoke deals sort after the public ladder
    await session.flush()

    # Clone the base plan's entitlements, then layer the negotiated overrides on top.
    base_ents = (
        await session.scalars(
            select(BillingPlanEntitlement).where(
                BillingPlanEntitlement.plan_id == base_plan_id
            )
        )
    ).all()
    existing = {
        e.capability_id: e
        for e in (
            await session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == plan_id
                )
            )
        ).all()
    }
    overrides = entitlement_overrides or {}
    cloned = 0
    for src in base_ents:
        row = existing.get(src.capability_id)
        if row is None:
            row = BillingPlanEntitlement(
                plan_id=plan_id, capability_id=src.capability_id
            )
            session.add(row)
            existing[src.capability_id] = row
        for field in (
            "mode", "quota", "soft_limit_pct", "hard_limit", "reset_policy",
            "burst_limit", "rate_limit", "cooldown_s", "overage_price_credits",
            "feature_flag", "trial_quota",
        ):
            setattr(row, field, getattr(src, field))
        cloned += 1

    applied = 0
    for capability_id, fields in overrides.items():
        row = existing.get(capability_id)
        if row is None:
            row = BillingPlanEntitlement(plan_id=plan_id, capability_id=capability_id)
            session.add(row)
            existing[capability_id] = row
        for field, value in fields.items():
            if hasattr(row, field):
                setattr(row, field, value)
        applied += 1
    await session.flush()

    provider_refs: dict = {}
    if publish_to_provider:
        from nexus.billing.payments import get_payment_provider

        try:
            provider_refs = await get_payment_provider().ensure_plan_price(
                plan_id=plan_id, name=plan.name, amount_cents=plan.base_price_cents,
                currency=plan.currency, interval=plan.interval,
            )
            plan.meta = {**(plan.meta or {}), **provider_refs}
            await session.flush()
        except Exception as exc:
            # The plan is still valid locally; publishing can be retried. Failing the whole
            # request would leave a half-built deal behind.
            logger.error("failed to publish %s to the payment provider", plan_id, exc_info=True)
            provider_refs = {"error": str(exc)[:200]}

    return {
        "plan_id": plan_id,
        "created": created,
        "base_plan_id": base_plan_id,
        "base_price_cents": plan.base_price_cents,
        "included_credits": plan.included_credits,
        "entitlements_cloned": cloned,
        "overrides_applied": applied,
        "provider": provider_refs,
    }
