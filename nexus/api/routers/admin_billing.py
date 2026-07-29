# nexus/api/routers/admin_billing.py
"""Staff-only billing administration (read surface for M1).

Everything here is gated by ``require_platform_admin`` — tenant RBAC grants no access.
Write endpoints (plan CRUD, entitlement editing, enforcement flips) land in Milestone 3
(docs/billing/06-Admin-Portal.md).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from nexus.api.deps import Principal, require_platform_admin
from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCapability, BillingPlan, BillingPlanEntitlement

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])


class CapabilityOut(BaseModel):
    id: str
    category: str
    sub_category: str
    name: str
    description: str
    unit: str
    meter_kind: str
    default_mode: str
    depends_on: list[str]
    active: bool


class PlanOut(BaseModel):
    id: str
    name: str
    description: str
    plan_class: str
    status: str
    base_price_cents: int
    seat_price_cents: int
    currency: str
    interval: str
    included_credits: int
    max_seats: int | None
    trial_days: int
    sort_order: int
    entitlement_count: int


@router.get("/capabilities", response_model=list[CapabilityOut])
async def list_capabilities(
    category: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    _: Principal = Depends(require_platform_admin),
) -> list[CapabilityOut]:
    stmt = select(BillingCapability)
    if category:
        stmt = stmt.where(BillingCapability.category == category)
    if active is not None:
        stmt = stmt.where(BillingCapability.active == active)
    stmt = stmt.order_by(BillingCapability.category, BillingCapability.id).limit(limit)
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(stmt)).all()
    return [
        CapabilityOut(
            id=c.id, category=c.category, sub_category=c.sub_category, name=c.name,
            description=c.description, unit=c.unit, meter_kind=c.meter_kind,
            default_mode=c.default_mode, depends_on=list(c.depends_on or []), active=c.active,
        )
        for c in rows
    ]


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    status_filter: str | None = None,
    _: Principal = Depends(require_platform_admin),
) -> list[PlanOut]:
    stmt = select(BillingPlan).order_by(BillingPlan.sort_order, BillingPlan.id)
    if status_filter:
        stmt = stmt.where(BillingPlan.status == status_filter)
    async with get_sessionmaker()() as session:
        plans = (await session.scalars(stmt)).all()
        counts = dict(
            (
                await session.execute(
                    select(
                        BillingPlanEntitlement.plan_id,
                        func.count(BillingPlanEntitlement.id),
                    ).group_by(BillingPlanEntitlement.plan_id)
                )
            ).all()
        )
    return [
        PlanOut(
            id=p.id, name=p.name, description=p.description, plan_class=p.plan_class,
            status=p.status, base_price_cents=p.base_price_cents,
            seat_price_cents=p.seat_price_cents, currency=p.currency, interval=p.interval,
            included_credits=p.included_credits, max_seats=p.max_seats,
            trial_days=p.trial_days, sort_order=p.sort_order,
            entitlement_count=int(counts.get(p.id, 0)),
        )
        for p in plans
    ]


class RateCardOut(BaseModel):
    capability_id: str
    name: str
    category: str
    unit: str
    credits_per_unit: float
    unit_cost_usd: float
    gross_margin: float
    tiers: list[dict]
    margin_exception: bool
    margin_exception_reason: str
    active: bool


class SubscriptionOut(BaseModel):
    tenant_id: str
    tenant_name: str
    plan_id: str
    status: str
    grandfathered: bool
    current_period_end: str | None


@router.get("/rates", response_model=list[RateCardOut])
async def list_rate_cards(
    _: Principal = Depends(require_platform_admin),
) -> list[RateCardOut]:
    """Every priced capability with its cost and the resulting gross margin.

    Margin is computed here rather than stored so it can never go stale against a repriced card
    or a revised cost — the number an admin sees is the number the guardrail enforces.
    """
    from nexus.billing.rates import gross_margin
    from nexus.models.billing import BillingCostRate, BillingRateCard

    async with get_sessionmaker()() as session:
        cards = (await session.scalars(select(BillingRateCard))).all()
        costs = {
            c.capability_id: float(c.unit_cost_usd)
            for c in (await session.scalars(select(BillingCostRate))).all()
        }
        caps = {
            c.id: c for c in (await session.scalars(select(BillingCapability))).all()
        }

    out: list[RateCardOut] = []
    for card in cards:
        cap = caps.get(card.capability_id)
        cost = costs.get(card.capability_id, 0.0)
        credits = float(card.credits_per_unit)
        out.append(
            RateCardOut(
                capability_id=card.capability_id,
                name=cap.name if cap else card.capability_id,
                category=cap.category if cap else "",
                unit=cap.unit if cap else "action",
                credits_per_unit=credits,
                unit_cost_usd=cost,
                gross_margin=round(gross_margin(credits, cost), 4),
                tiers=list(card.tiers or []),
                margin_exception=card.margin_exception,
                margin_exception_reason=card.margin_exception_reason,
                active=card.active,
            )
        )
    out.sort(key=lambda r: (r.category, r.capability_id))
    return out


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(
    _: Principal = Depends(require_platform_admin),
) -> list[SubscriptionOut]:
    """Every tenant's current plan. Reads across tenants deliberately — this is the platform
    control plane, not a tenant surface, and it is gated by `require_platform_admin`."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription
    from nexus.models.identity import Tenant

    # Cross-tenant by definition. Under the app's RLS-bound role this query returns zero rows
    # rather than erroring, which is why the staff console looked empty on a database holding
    # eleven subscriptions.
    async with get_platform_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(BillingSubscription, Tenant.name).join(
                    Tenant, Tenant.id == BillingSubscription.tenant_id
                )
            )
        ).all()

    return sorted(
        (
            SubscriptionOut(
                tenant_id=sub.tenant_id, tenant_name=name, plan_id=sub.plan_id,
                status=sub.status, grandfathered=sub.grandfathered,
                current_period_end=(
                    sub.current_period_end.isoformat() if sub.current_period_end else None
                ),
            )
            for sub, name in rows
        ),
        key=lambda s: s.tenant_name.lower(),
    )
