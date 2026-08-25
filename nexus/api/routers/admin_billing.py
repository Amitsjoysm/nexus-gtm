# nexus/api/routers/admin_billing.py
"""Staff-only billing administration (read surface).

Gated on ``billing.read``, which every platform role holds — tenant RBAC grants no access at all.
The one exception is the ``/admins`` listing: who operates the platform is only visible to admins
who can change it (``admins.manage``). Writes live in ``admin_billing_write.py``.

``whoami`` is deliberately gated on plain authentication instead, because it answers "am I one?"
and must return false rather than 403.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from nexus.api.deps import Principal, get_principal, require_platform_permission
from nexus.billing.permissions import ADMINS_MANAGE, BILLING_READ, effective_permissions
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


@router.get("/plans/{plan_id}/entitlements")
async def list_plan_entitlements(
    plan_id: str,
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> list[dict]:
    """Every capability, with this plan's entitlement where one exists.

    Returns the FULL capability list rather than only the configured rows. An editor that shows
    only what is already set cannot be used to add anything, and "this plan says nothing about
    ai.email_draft" is the state an operator most needs to see — an unconfigured capability falls
    through to the catalog default, which is easy to forget and impossible to notice from a list
    that omits it.
    """
    from sqlalchemy import select as _select

    from nexus.models.billing import BillingCapability, BillingPlan, BillingPlanEntitlement

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_id}'")
        caps = (
            await session.scalars(
                _select(BillingCapability)
                .where(BillingCapability.active == True)  # noqa: E712
                .order_by(BillingCapability.category, BillingCapability.id)
            )
        ).all()
        ents = {
            e.capability_id: e
            for e in (
                await session.scalars(
                    _select(BillingPlanEntitlement).where(
                        BillingPlanEntitlement.plan_id == plan_id
                    )
                )
            ).all()
        }

    out = []
    for cap in caps:
        ent = ents.get(cap.id)
        out.append({
            "capability_id": cap.id,
            "name": cap.name,
            "category": cap.category,
            "unit": cap.unit,
            "default_mode": cap.default_mode,
            "configured": ent is not None,
            "mode": ent.mode if ent else None,
            "quota": ent.quota if ent else None,
            "soft_limit_pct": ent.soft_limit_pct if ent else 80,
            "overage_price_credits": ent.overage_price_credits if ent else None,
            "feature_flag": ent.feature_flag if ent else None,
        })
    return out


@router.get("/capabilities", response_model=list[CapabilityOut])
async def list_capabilities(
    category: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
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
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
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
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
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
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
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


class WhoAmIOut(BaseModel):
    email: str
    is_platform_admin: bool
    platform_role: str = ""
    # Lets the SPA hide controls a narrower role cannot use. The server is still the boundary.
    permissions: list[str] = []
    # The ceiling on this caller's credit grants, or null for no ceiling. Surfaced so the console
    # can state the actual limit instead of hardcoding a number that config can change.
    credit_grant_cap: float | None = None


@router.get("/whoami", response_model=WhoAmIOut)
async def whoami(principal: Principal = Depends(get_principal)) -> WhoAmIOut:
    """Is the caller a platform admin?

    Deliberately gated on plain authentication, not on require_platform_admin: it answers
    "am I one?", so it must return false rather than 403. The SPA uses it to decide whether the
    staff console is reachable at all, instead of guessing from tenant role — a workspace owner
    is not a platform admin, and routing on tenant role let any owner load the console shell.
    """
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.models.billing import PlatformAdmin
    from nexus.models.identity import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, principal.user_id)
        email = (user.email or "").lower() if user else ""
        if not email:
            return WhoAmIOut(email="", is_platform_admin=False)
        if email in get_settings().platform_admin_email_list:
            from nexus.billing.permissions import ALL_PERMISSIONS

            return WhoAmIOut(
                email=email, is_platform_admin=True, platform_role="superadmin",
                permissions=sorted(ALL_PERMISSIONS), credit_grant_cap=None,
            )
        row = (
            await session.scalars(
                select(PlatformAdmin).where(
                    PlatformAdmin.email == email, PlatformAdmin.active == True  # noqa: E712
                )
            )
        ).first()
    if row is None:
        return WhoAmIOut(email=email, is_platform_admin=False)
    from nexus.billing.permissions import CREDITS_GRANT, effective_permissions

    held = effective_permissions(row)
    return WhoAmIOut(
        email=email, is_platform_admin=True, platform_role=row.platform_role,
        permissions=sorted(held),
        # None means "no ceiling". Whether the caller can grant at all is answered by the
        # permission list, not by this field.
        credit_grant_cap=(
            None if CREDITS_GRANT in held else get_settings().billing_support_credit_cap
        ),
    )


@router.get("/revenue")
async def revenue_report(
    since: str = Query(default="", description="Period key floor, e.g. 2026-01"),
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> dict:
    """MRR/ARR, plan mix, and collection health.

    Derived at read time from subscriptions, plans and invoices — no stored aggregate to drift from
    the rows it describes, and no scheduled job to fall behind. Cross-tenant by nature, so it runs
    through the platform sessionmaker; under the RLS-bound role it would silently return zero rows
    and report an MRR of $0.
    """
    from nexus.billing.revenue import collection_health, snapshot

    revenue = await snapshot()
    collection = await collection_health(since=since)
    return {"revenue": revenue.as_dict(), "collection": collection.as_dict()}


class PlatformAdminOut(BaseModel):
    id: str
    email: str
    platform_role: str
    permissions: list[str] = []
    active: bool
    note: str
    created_at: str


@router.get("/admins", response_model=list[PlatformAdminOut])
async def list_platform_admins(
    _: Principal = Depends(require_platform_permission(ADMINS_MANAGE)),
) -> list[PlatformAdminOut]:
    from nexus.models.billing import PlatformAdmin

    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(PlatformAdmin).order_by(PlatformAdmin.email))
        ).all()
    return [
        PlatformAdminOut(
            id=r.id, email=r.email, platform_role=r.platform_role,
            permissions=sorted(effective_permissions(r)), active=r.active,
            note=r.note, created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


# ---- platform overview: how many users, and how much are they actually using? --------------------
#
# Neither number existed anywhere. The Subscriptions tab listed plan and status, `/billing/usage` is
# tenant-scoped, and `/admin/users/{email}/activity` answers for one person at a time — so "how many
# users do we have and what are they consuming" could only be answered with SQL.


class PlatformOverviewOut(BaseModel):
    users: int
    active_users: int
    tenants: int
    # Metered actions this period and all-time. The period number is what a capacity conversation
    # needs; the all-time number is what a pricing one needs.
    requests_this_period: int
    requests_total: int
    # Attribution is PARTIAL by construction and the UI must say so: only `billing_usage_events`
    # carries a `user_id`, and only when the call arrived through a request with a principal.
    # Background work — crawls, sweeps, plays — is real usage with nobody to attribute it to.
    requests_with_a_user: int
    credits_granted: float
    credits_spent: float


@router.get("/overview", response_model=PlatformOverviewOut)
async def platform_overview(
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> PlatformOverviewOut:
    """Platform-wide counts, in one round trip of scalar subqueries."""
    from sqlalchemy import func, select as _sel

    from nexus.billing.rollups import period_start
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingCreditLedger, BillingUsageEvent
    from nexus.models.identity import Tenant, User

    # `billing_usage_events` has no period column — the period is a time window over
    # `occurred_at`, the same way /billing/usage computes it.
    since = period_start(utcnow())

    def _count(model, *where):
        return _sel(func.count()).select_from(model).where(*where).scalar_subquery() \
            if where else _sel(func.count()).select_from(model).scalar_subquery()

    # The PLATFORM sessionmaker, not the app one. `billing_usage_events` and
    # `billing_credit_ledger` are tenant-scoped, so under the RLS-bound app role a cross-tenant
    # aggregate returns ZERO ROWS rather than an error — it reported `requests_total: 0` against a
    # database holding 18 events, which reads as "nobody has used anything".
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as session:
        row = (
            await session.execute(
                _sel(
                    _count(User).label("users"),
                    _count(Tenant).label("tenants"),
                    _count(BillingUsageEvent).label("requests_total"),
                    _count(BillingUsageEvent,
                           BillingUsageEvent.occurred_at >= since).label("requests_period"),
                    _count(BillingUsageEvent,
                           BillingUsageEvent.user_id.isnot(None)).label("requests_user"),
                    _sel(func.coalesce(func.sum(BillingCreditLedger.delta), 0.0))
                    .where(BillingCreditLedger.delta > 0).scalar_subquery().label("granted"),
                    _sel(func.coalesce(func.sum(BillingCreditLedger.delta), 0.0))
                    .where(BillingCreditLedger.delta < 0).scalar_subquery().label("spent"),
                )
            )
        ).one()
        # `is_active` may not exist on every deployment's User model; count all users as active
        # when it does not rather than reporting zero.
        active = row.users
        if hasattr(User, "is_active"):
            active = int(
                (await session.execute(
                    _sel(func.count()).select_from(User).where(User.is_active.is_(True))
                )).scalar() or 0
            )

    return PlatformOverviewOut(
        users=int(row.users or 0),
        active_users=int(active or 0),
        tenants=int(row.tenants or 0),
        requests_this_period=int(row.requests_period or 0),
        requests_total=int(row.requests_total or 0),
        requests_with_a_user=int(row.requests_user or 0),
        credits_granted=float(row.granted or 0.0),
        credits_spent=abs(float(row.spent or 0.0)),
    )


# ---- the customer directory ----------------------------------------------------------------------
# One place to find a workspace — by the email of anyone in it, or by its own name — and see what
# it is on, what it has used, and what it owes. Before this the answer lived in four screens: the
# Subscriptions tab knew the plan, /billing/usage was tenant-scoped and answered only for the
# caller's own workspace, credits were visible nowhere outside a dialog, and "which workspace is
# this person in?" had no surface at all.


class CustomerRowOut(BaseModel):
    tenant_id: str
    workspace: str
    plan_id: str
    plan_name: str
    status: str
    users: int
    # Whoever the search matched, so an operator who typed an email can see they found the right
    # person rather than a workspace that merely contains someone with a similar address.
    matched_email: str = ""
    requests_this_period: int
    credits_balance: float


@router.get("/customers", response_model=list[CustomerRowOut])
async def list_customers(
    q: str = "",
    limit: int = 50,
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> list[CustomerRowOut]:
    """Search workspaces by name, slug, or the email of any member.

    Runs on the PLATFORM sessionmaker: every table read here except `tenants` and `users` is
    tenant-scoped, so under the RLS-bound app role this returns zero rows rather than an error.
    That is the documented trap, and this subsystem has already walked into it twice.
    """
    from sqlalchemy import func, or_, select as _sel

    from nexus.billing.rollups import period_start
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import (
        BillingCreditLedger,
        BillingPlan,
        BillingSubscription,
        BillingUsageEvent,
    )
    from nexus.models.identity import Membership, Tenant, User

    q = (q or "").strip()
    limit = max(1, min(int(limit or 50), 200))
    since = period_start(utcnow())

    async with get_platform_sessionmaker()() as session:
        stmt = _sel(Tenant)
        matched: dict[str, str] = {}
        if q:
            like = f"%{q}%"
            # An email match resolves through membership, so searching for a person finds the
            # workspace they are in — which is the question an operator granting credits actually
            # has. Credits belong to a workspace, not to a person; the row says which one.
            member_rows = (
                await session.execute(
                    _sel(Membership.tenant_id, User.email)
                    .join(User, User.id == Membership.user_id)
                    .where(User.email.ilike(like))
                )
            ).all()
            matched = {row[0]: row[1] for row in member_rows}
            clauses = [Tenant.name.ilike(like), Tenant.slug.ilike(like)]
            if matched:
                clauses.append(Tenant.id.in_(list(matched)))
            stmt = stmt.where(or_(*clauses))
        tenants = list((await session.scalars(stmt.limit(limit))).all())
        if not tenants:
            return []
        ids = [t.id for t in tenants]

        def _grouped(expr, model, *where):
            return (
                _sel(model.tenant_id, expr)
                .where(model.tenant_id.in_(ids), *where)
                .group_by(model.tenant_id)
            )

        subs = {
            s.tenant_id: s
            for s in (await session.scalars(
                _sel(BillingSubscription).where(BillingSubscription.tenant_id.in_(ids))
            )).all()
        }
        seats = dict((await session.execute(_grouped(func.count(), Membership))).all())
        reqs = dict((await session.execute(
            _grouped(func.count(), BillingUsageEvent, BillingUsageEvent.occurred_at >= since)
        )).all())
        credits = dict((await session.execute(
            _grouped(func.coalesce(func.sum(BillingCreditLedger.delta), 0.0), BillingCreditLedger)
        )).all())
        # Plans live in a TABLE, not a module constant: a custom deal is seeded per tenant and
        # would render as a blank name if this read the built-in catalog.
        plans = {
            p.id: p
            for p in (await session.scalars(
                _sel(BillingPlan).where(
                    BillingPlan.id.in_([s.plan_id for s in subs.values() if s.plan_id] or [""])
                )
            )).all()
        }

    out: list[CustomerRowOut] = []
    for t in tenants:
        sub = subs.get(t.id)
        plan_id = sub.plan_id if sub else ""
        plan = plans.get(plan_id)
        out.append(CustomerRowOut(
            tenant_id=t.id,
            workspace=t.name or t.slug or t.id,
            plan_id=plan_id,
            # Falls back to the id, so a plan the catalog does not know still names itself rather
            # than rendering blank for exactly the deals an operator most wants to find.
            plan_name=(plan.name if plan is not None else plan_id) or "-",
            status=sub.status if sub else "none",
            users=int(seats.get(t.id, 0)),
            matched_email=matched.get(t.id, ""),
            requests_this_period=int(reqs.get(t.id, 0)),
            credits_balance=float(credits.get(t.id, 0.0) or 0.0),
        ))
    out.sort(key=lambda r: (-r.requests_this_period, r.workspace.lower()))
    return out


class TenantUsageOut(BaseModel):
    tenant_id: str
    workspace: str
    period: str
    plan_id: str
    plan_name: str
    status: str
    capabilities: list[dict]
    credits_balance: float
    requests_this_period: int
    requests_total: int


@router.get("/customers/{tenant_id}/usage", response_model=TenantUsageOut)
async def customer_usage(
    tenant_id: str,
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> TenantUsageOut:
    """What one workspace has consumed this period, read from the platform role.

    The customer's own `/billing/usage` answers the same question for the caller's workspace only.
    An operator investigating a bill, or deciding whether a goodwill grant is warranted, cannot
    reach it — so "how much has this customer used?" had no answer short of impersonating them.
    """
    from sqlalchemy import func, select as _sel

    from nexus.billing.rollups import period_key, period_start
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import (
        BillingCapability,
        BillingCreditLedger,
        BillingPlan,
        BillingSubscription,
        BillingUsageEvent,
    )
    from nexus.models.identity import Tenant

    now = utcnow()
    since = period_start(now)

    async with get_platform_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such workspace")
        sub = (await session.scalars(
            _sel(BillingSubscription).where(BillingSubscription.tenant_id == tenant_id)
        )).first()

        used = dict((await session.execute(
            _sel(BillingUsageEvent.capability_id,
                 func.coalesce(func.sum(BillingUsageEvent.quantity), 0.0))
            .where(BillingUsageEvent.tenant_id == tenant_id,
                   BillingUsageEvent.occurred_at >= since)
            .group_by(BillingUsageEvent.capability_id)
        )).all())
        totals = (await session.execute(
            _sel(func.count()).select_from(BillingUsageEvent)
            .where(BillingUsageEvent.tenant_id == tenant_id)
        )).scalar() or 0
        period_reqs = (await session.execute(
            _sel(func.count()).select_from(BillingUsageEvent)
            .where(BillingUsageEvent.tenant_id == tenant_id,
                   BillingUsageEvent.occurred_at >= since)
        )).scalar() or 0
        balance = (await session.execute(
            _sel(func.coalesce(func.sum(BillingCreditLedger.delta), 0.0))
            .where(BillingCreditLedger.tenant_id == tenant_id)
        )).scalar() or 0.0
        plan = await session.get(BillingPlan, sub.plan_id) if sub and sub.plan_id else None
        catalog = {
            c.id: c
            for c in (await session.scalars(
                _sel(BillingCapability).where(BillingCapability.id.in_(list(used) or [""]))
            )).all()
        }

    caps = [
        {
            "capability_id": cid,
            # Falls back to the id rather than an empty label: a capability metered before it was
            # seeded is a real state, and a blank name would read as a rendering bug.
            "name": getattr(catalog.get(cid), "name", "") or cid,
            "category": getattr(catalog.get(cid), "category", "") or "",
            "used": float(qty or 0.0),
        }
        # Only what has actually been used. Listing the whole catalog at zero would bury the
        # handful of capabilities this customer touches under sixty rows of nothing.
        for cid, qty in sorted(used.items(), key=lambda kv: -float(kv[1] or 0))
    ]

    return TenantUsageOut(
        tenant_id=tenant_id,
        workspace=tenant.name or tenant.slug or tenant_id,
        period=period_key(now, "period"),
        plan_id=sub.plan_id if sub else "",
        plan_name=(plan.name if plan is not None else (sub.plan_id if sub else "")) or "-",
        status=sub.status if sub else "none",
        capabilities=caps,
        credits_balance=float(balance),
        requests_this_period=int(period_reqs),
        requests_total=int(totals),
    )
