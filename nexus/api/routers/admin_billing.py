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
