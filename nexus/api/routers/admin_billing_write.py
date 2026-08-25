# nexus/api/routers/admin_billing_write.py
"""Staff-only billing administration (write surface).

Pricing must be changeable without a redeploy — that is the whole premise of the platform
(docs/billing/06-Admin-Portal.md §2). Every endpoint here names the one permission it needs via
``require_platform_permission`` — it fails closed, and no tenant role can reach it. Being a
platform admin is not enough: repricing is `pricing.write`, moving a workspace between plans is
`subscriptions.write`, charging a card is `invoices.collect`.

Two invariants are enforced at this layer, not merely documented:

* **The margin floor.** A rate card write is validated against the stored cost rate and refused
  with 422 unless finance records an explicit exception. An admin must not be able to click past
  a below-cost price.
* **Idempotency on money.** Granting credits requires a caller-supplied idempotency key, so a
  double-clicked button cannot double-grant.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_principal, require_platform_permission
from nexus.billing.audit import record_admin_action, snapshot
from nexus.billing.permissions import ALL_PERMISSIONS, permissions_for_role, ADMINS_MANAGE, BILLING_READ, INVOICES_COLLECT, PRICING_WRITE, SUBSCRIPTIONS_WRITE
from nexus.core.db import get_sessionmaker
from nexus.core.tenancy import TenantSession, apply_rls
from nexus.models.billing import (
    BillingCapability,
    BillingCostRate,
    BillingPlan,
    BillingPlanEntitlement,
    BillingRateCard,
)

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])

# Snapshotted into the audit log so a dispute can be reconstructed from the record alone.
_PLAN_FIELDS = (
    "name", "status", "base_price_cents", "seat_price_cents", "included_credits",
    "max_seats", "trial_days", "sort_order",
)
_FLAG_FIELDS = ("description", "enabled", "overrides")
_ENT_FIELDS = (
    "mode", "quota", "soft_limit_pct", "hard_limit", "burst_limit",
    "overage_price_credits", "reset_policy",
)
_RATE_FIELDS = ("credits_per_unit", "active", "margin_exception", "margin_exception_reason")


class PlanPatch(BaseModel):
    """Partial plan update. Only commercial fields are mutable — ``id`` and ``plan_class`` are
    identity, and changing them would silently reinterpret existing subscriptions."""

    model_config = {"extra": "forbid"}

    name: str | None = None
    description: str | None = None
    status: str | None = None
    base_price_cents: int | None = Field(default=None, ge=0)
    seat_price_cents: int | None = Field(default=None, ge=0)
    included_credits: int | None = Field(default=None, ge=0)
    max_seats: int | None = Field(default=None, ge=0)
    trial_days: int | None = Field(default=None, ge=0)
    sort_order: int | None = None


class EntitlementIn(BaseModel):
    model_config = {"extra": "forbid"}

    mode: str = "metered"
    quota: int | None = None
    soft_limit_pct: int = 80
    hard_limit: int | None = None
    reset_policy: str = "monthly_anniversary"
    burst_limit: int | None = None
    rate_limit: str | None = None
    cooldown_s: int | None = None
    overage_price_credits: int | None = None
    feature_flag: str | None = None
    trial_quota: int | None = None


class RateCardIn(BaseModel):
    model_config = {"extra": "forbid"}

    credits_per_unit: float = Field(ge=0)
    tiers: list[dict] = Field(default_factory=list)
    active: bool = True
    margin_exception: bool = False
    margin_exception_reason: str = ""


class SubscriptionIn(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str


class FeatureFlagIn(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    description: str = ""


class FeatureFlagOverrideIn(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool


class PauseIn(BaseModel):
    """Why a subscription was paused. Optional, but it lands in the audit log — a pause with no
    stated reason is the one support cannot explain three months later."""

    model_config = {"extra": "forbid"}

    reason: str = ""


class CreditGrantIn(BaseModel):
    model_config = {"extra": "forbid"}

    amount: float = Field(gt=0)
    reason: str = ""
    # Required, not optional: a retried request must not mint credits twice.
    idempotency_key: str = Field(min_length=1, max_length=120)


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    body: PlanPatch,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    async with get_sessionmaker()() as session:
        plan = await session.get(BillingPlan, plan_id)
        if plan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_id}'")
        before = snapshot(plan, _PLAN_FIELDS)
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(plan, field, value)
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="plan.update", target=plan_id,
            before=before, after=snapshot(plan, _PLAN_FIELDS),
        )
        await session.commit()
        return {
            "id": plan.id, "name": plan.name, "status": plan.status,
            "base_price_cents": plan.base_price_cents,
            "seat_price_cents": plan.seat_price_cents,
            "included_credits": plan.included_credits,
        }


@router.put("/plans/{plan_id}/entitlements/{capability_id}")
async def upsert_entitlement(
    plan_id: str,
    capability_id: str,
    body: EntitlementIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_id}'")
        if await session.get(BillingCapability, capability_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Unknown capability '{capability_id}'"
            )
        row = (
            await session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == plan_id,
                    BillingPlanEntitlement.capability_id == capability_id,
                )
            )
        ).first()
        before = snapshot(row, _ENT_FIELDS)
        if row is None:
            row = BillingPlanEntitlement(plan_id=plan_id, capability_id=capability_id)
            session.add(row)
        for field, value in body.model_dump().items():
            setattr(row, field, value)
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="entitlement.upsert",
            target=f"{plan_id}/{capability_id}", before=before,
            after=snapshot(row, _ENT_FIELDS),
        )
        await session.commit()
        return {"plan_id": plan_id, "capability_id": capability_id, "mode": row.mode,
                "quota": row.quota}


@router.put("/rates/{capability_id}")
async def upsert_rate_card(
    capability_id: str,
    body: RateCardIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Set a capability's price. Refused with 422 below the margin floor.

    The same ``validate_rate`` guard that runs on the seed runs here, so there is no path — seed
    or admin — that lands an underwater price in the database without an explicit exception.
    """
    from nexus.billing.rates import MarginFloorError, gross_margin, validate_rate

    async with get_sessionmaker()() as session:
        if await session.get(BillingCapability, capability_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Unknown capability '{capability_id}'"
            )
        cost = await session.get(BillingCostRate, capability_id)
        unit_cost = float(cost.unit_cost_usd) if cost is not None else 0.0
        try:
            validate_rate(
                capability_id,
                credits_per_unit=body.credits_per_unit,
                unit_cost_usd=unit_cost,
                margin_exception=body.margin_exception,
            )
        except MarginFloorError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
            ) from exc

        card = await session.get(BillingRateCard, capability_id)
        before = snapshot(card, _RATE_FIELDS)
        if card is None:
            card = BillingRateCard(capability_id=capability_id)
            session.add(card)
        card.credits_per_unit = body.credits_per_unit
        card.tiers = body.tiers
        card.active = body.active
        card.margin_exception = body.margin_exception
        card.margin_exception_reason = body.margin_exception_reason
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="rate.upsert", target=capability_id,
            before=before, after=snapshot(card, _RATE_FIELDS),
            note=body.margin_exception_reason,
        )
        await session.commit()
        return {
            "capability_id": capability_id,
            "credits_per_unit": body.credits_per_unit,
            "unit_cost_usd": unit_cost,
            "gross_margin": round(gross_margin(body.credits_per_unit, unit_cost), 4),
            "margin_exception": body.margin_exception,
        }


@router.post("/tenants/{tenant_id}/subscription")
async def set_tenant_subscription(
    tenant_id: str,
    body: SubscriptionIn,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    from nexus.billing.subscriptions import change_plan, ensure_subscription

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, body.plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{body.plan_id}'")
        # A platform admin acts ACROSS tenants, so there is no request-scoped tenant binding to
        # inherit. Bind it explicitly or Postgres RLS rejects the write.
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        created = await ensure_subscription(ts, plan_id=body.plan_id)
        sub = created if created is not None else await change_plan(
            ts, body.plan_id, actor=principal.user_id
        )
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.change",
            target=body.plan_id, subject_tenant_id=tenant_id,
            after={"plan_id": sub.plan_id, "status": sub.status},
        )
        await session.commit()
        return {"tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status}


@router.get("/flags")
async def list_feature_flags(
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> list[dict]:
    """Every flag, with the plans whose entitlements name it.

    The usage list is the point. A flag nobody references is safe to delete or flip; one wired into
    a paid plan's entitlement is a switch that turns a customer's feature off, and an operator
    should not have to grep the catalog to tell those two apart.
    """
    from sqlalchemy import select as _select

    from nexus.models.billing import BillingFeatureFlag

    async with get_sessionmaker()() as session:
        flags = (
            await session.scalars(_select(BillingFeatureFlag).order_by(BillingFeatureFlag.id))
        ).all()
        ents = (
            await session.scalars(
                _select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.feature_flag.is_not(None)
                )
            )
        ).all()

    used: dict[str, set[str]] = {}
    for e in ents:
        used.setdefault(e.feature_flag or "", set()).add(e.plan_id)

    return [
        {
            "id": f.id,
            "description": f.description,
            "enabled": f.enabled,
            "overrides": f.overrides or {},
            "used_by_plans": sorted(used.get(f.id, set())),
        }
        for f in flags
    ]


@router.put("/flags/{flag_id}")
async def upsert_feature_flag(
    flag_id: str,
    body: FeatureFlagIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Create or update a flag's default. Idempotent — the name is the primary key."""
    from nexus.models.billing import BillingFeatureFlag

    async with get_sessionmaker()() as session:
        flag = await session.get(BillingFeatureFlag, flag_id)
        before = snapshot(flag, _FLAG_FIELDS) if flag is not None else None
        if flag is None:
            flag = BillingFeatureFlag(id=flag_id, overrides={})
            session.add(flag)
        flag.enabled = body.enabled
        if body.description:
            flag.description = body.description
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="flag.upsert", target=flag_id,
            before=before, after=snapshot(flag, _FLAG_FIELDS), note=body.description,
        )
        await session.commit()
        return {
            "id": flag.id, "description": flag.description, "enabled": flag.enabled,
            "overrides": flag.overrides or {},
        }


@router.put("/flags/{flag_id}/overrides/{scope}/{key}")
async def set_feature_flag_override(
    flag_id: str,
    scope: str,
    key: str,
    body: FeatureFlagOverrideIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Override a flag for one tenant or one environment.

    Stored on the flag rather than as its own row so "is this flag on anywhere?" stays one lookup —
    see the model docstring. Scope is whitelisted because the key format is what
    ``flags.flag_enabled`` parses; a typo'd scope would write an override that is never read.
    """
    from nexus.models.billing import BillingFeatureFlag

    if scope not in ("tenant", "env"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope must be tenant or env")

    async with get_sessionmaker()() as session:
        flag = await session.get(BillingFeatureFlag, flag_id)
        if flag is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown flag '{flag_id}'")
        before = snapshot(flag, _FLAG_FIELDS)
        # Reassign rather than mutate: SQLAlchemy does not track in-place changes to a JSON dict,
        # so `overrides[k] = v` would flush nothing and the override would silently not exist.
        flag.overrides = {**(flag.overrides or {}), f"{scope}:{key}": bool(body.enabled)}
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="flag.override.set",
            target=f"{flag_id}:{scope}:{key}",
            subject_tenant_id=key if scope == "tenant" else None,
            before=before, after=snapshot(flag, _FLAG_FIELDS),
        )
        await session.commit()
        return {"id": flag.id, "overrides": flag.overrides}


@router.delete("/flags/{flag_id}/overrides/{scope}/{key}")
async def clear_feature_flag_override(
    flag_id: str,
    scope: str,
    key: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Remove an override so the flag falls back to its default.

    Without this a beta grant is permanent: setting ``tenant:X`` to false would be the only way
    back, which is not the same thing as "follow the default from now on".
    """
    from nexus.models.billing import BillingFeatureFlag

    async with get_sessionmaker()() as session:
        flag = await session.get(BillingFeatureFlag, flag_id)
        if flag is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown flag '{flag_id}'")
        before = snapshot(flag, _FLAG_FIELDS)
        remaining = {k: v for k, v in (flag.overrides or {}).items() if k != f"{scope}:{key}"}
        flag.overrides = remaining
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="flag.override.clear",
            target=f"{flag_id}:{scope}:{key}",
            subject_tenant_id=key if scope == "tenant" else None,
            before=before, after=snapshot(flag, _FLAG_FIELDS),
        )
        await session.commit()
        return {"id": flag.id, "overrides": flag.overrides}


@router.get("/tenants/{tenant_id}/proration-preview")
async def preview_tenant_proration(
    tenant_id: str,
    plan_id: str,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """What moving this tenant to ``plan_id`` would credit and charge. Writes nothing.

    An admin changing a plan is committing real money on a customer's behalf. Being able to see
    the number first is the difference between a decision and a surprise on somebody's invoice.
    """
    from nexus.billing.subscriptions import preview_proration

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_id}'")
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        p = await preview_proration(ts, plan_id=plan_id)
        # Deliberately no commit: a preview that writes is not a preview.
        await session.rollback()

    return {
        "tenant_id": tenant_id,
        "plan_id": plan_id,
        "credit_cents": p.credit_cents,
        "charge_cents": p.charge_cents,
        "net_cents": p.net_cents,
        "days_remaining": p.days_remaining,
        "days_in_period": p.days_in_period,
    }


@router.post("/tenants/{tenant_id}/pause")
async def pause_tenant_subscription(
    tenant_id: str,
    body: PauseIn | None = None,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """Pause billing and access, keeping the plan terms and history intact.

    The alternative a customer asking to pause used to get was cancellation, which loses their
    negotiated terms and their timeline.
    """
    from nexus.billing.errors import BillingError
    from nexus.billing.subscriptions import pause_subscription

    reason = (body.reason if body else "") or ""
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        try:
            sub = await pause_subscription(ts, actor=principal.user_id)
        except BillingError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.pause",
            target=sub.plan_id, subject_tenant_id=tenant_id,
            before={"status": "active"}, after={"status": sub.status}, note=reason,
        )
        await session.commit()
        return {"tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status,
                "paused_at": (sub.meta or {}).get("paused_at")}


@router.post("/tenants/{tenant_id}/resume")
async def resume_tenant_subscription(
    tenant_id: str,
    body: PauseIn | None = None,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """Resume, pushing the period end out by however long the pause lasted."""
    from nexus.billing.errors import BillingError
    from nexus.billing.subscriptions import resume_subscription

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        try:
            sub = await resume_subscription(ts, actor=principal.user_id)
        except BillingError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.resume",
            target=sub.plan_id, subject_tenant_id=tenant_id,
            before={"status": "suspended"}, after={"status": sub.status},
            note=(body.reason if body else "") or "",
        )
        await session.commit()
        return {
            "tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status,
            "period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "days_returned": (sub.meta or {}).get("last_pause_days", 0),
        }


@router.post("/tenants/{tenant_id}/credits")
async def grant_tenant_credits(
    tenant_id: str,
    body: CreditGrantIn,
    principal: Principal = Depends(get_principal),
) -> dict:
    """Grant credits. Requires ``credits.grant``, or ``credits.grant.capped`` within the ceiling.

    The permission needed depends on the AMOUNT, so this cannot be a plain ``Depends`` gate:
    support may issue goodwill credits up to a configured ceiling, and anything larger needs
    finance. Checked here rather than in a dependency because only the body knows the size.
    """
    from nexus.api.deps import platform_permissions
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.permissions import CREDITS_GRANT, CREDITS_GRANT_CAPPED
    from nexus.core.config import get_settings

    held = await platform_permissions(principal)
    cap = get_settings().billing_support_credit_cap
    if CREDITS_GRANT not in held:
        if CREDITS_GRANT_CAPPED not in held:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        if body.amount > cap:
            # Deliberately specific: the caller has a legitimate grant permission and needs to
            # know an escalation is required, not be told they have no access at all.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"grants above {cap:g} credits require the credits.grant permission",
            )

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)          # cross-tenant admin write; see above
        ts = TenantSession(session, tenant_id)
        applied = await grant_credits(
            ts, body.amount, kind="adjustment", reason=body.reason,
            idempotency_key=body.idempotency_key, actor=principal.user_id,
        )
        await record_admin_action(
            session, actor=principal.user_id, action="credits.grant",
            target=body.idempotency_key, subject_tenant_id=tenant_id,
            after={"amount": body.amount, "applied": applied}, note=body.reason,
        )
        # Read the balance BEFORE committing. apply_rls sets the tenant GUC transaction-locally,
        # so after a commit the binding is gone and the very next read returns zero rows under
        # RLS — reporting "balance: 0" for a grant that actually succeeded.
        new_balance = await balance(ts)
        await session.commit()
        return {
            "tenant_id": tenant_id,
            # False means the key was already used — the caller does not need to distinguish
            # "refused" from "already applied"; both mean no new credits were minted.
            "applied": applied,
            "balance": new_balance,
        }


class CollectIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Used only when the workspace has no payment customer yet.
    email: str = ""
    name: str = ""


@router.post("/tenants/{tenant_id}/invoices/{invoice_id}/collect")
async def collect_invoice_endpoint(
    tenant_id: str,
    invoice_id: str,
    body: CollectIn,
    principal: Principal = Depends(require_platform_permission(INVOICES_COLLECT)),
) -> dict:
    """Charge a finalized invoice through the configured payment provider.

    Explicit rather than automatic on period close: nobody should discover they were charged
    because a background job decided so. Auto-collection is a separate decision to make once
    dunning exists.
    """
    from nexus.billing.collection import CollectionError, collect_invoice

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        try:
            result = await collect_invoice(
                ts, invoice_id, email=body.email, name=body.name
            )
        except CollectionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="invoice.collect",
            target=invoice_id, subject_tenant_id=tenant_id, after=result,
        )
        await session.commit()
        return result


class CustomPlanIn(BaseModel):
    """A negotiated, per-customer deal."""

    model_config = {"extra": "forbid"}

    base_plan_id: str = "growth"
    name: str = ""
    base_price_cents: int = Field(ge=0)
    included_credits: int = Field(default=0, ge=0)
    currency: str = "USD"
    interval: str = "month"
    max_seats: int | None = Field(default=None, ge=0)
    # capability_id -> {quota, mode, overage_price_credits, ...}
    entitlement_overrides: dict[str, dict] = Field(default_factory=dict)
    # Assign it to the tenant immediately. False builds the deal without switching them onto it.
    assign: bool = True
    publish_to_provider: bool = True


@router.post("/tenants/{tenant_id}/custom-plan")
async def create_tenant_custom_plan(
    tenant_id: str,
    body: CustomPlanIn,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """Build a bespoke plan for one customer and publish it to the payment provider.

    Clones a base plan so a deal never has to be specified from scratch, applies the negotiated
    overrides, creates the product + price at the PSP, and (by default) moves the tenant onto it.
    """
    from nexus.billing.custom_plans import CustomPlanError, create_custom_plan, custom_plan_id
    from nexus.billing.subscriptions import change_plan, ensure_subscription
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tenant '{tenant_id}'")

        plan_id = custom_plan_id(tenant.slug)
        try:
            result = await create_custom_plan(
                session,
                plan_id=plan_id,
                name=body.name or f"{tenant.name} (custom)",
                base_plan_id=body.base_plan_id,
                base_price_cents=body.base_price_cents,
                included_credits=body.included_credits,
                currency=body.currency,
                interval=body.interval,
                max_seats=body.max_seats,
                entitlement_overrides=body.entitlement_overrides,
                publish_to_provider=body.publish_to_provider,
            )
        except CustomPlanError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        if body.assign:
            await apply_rls(session, tenant_id)
            ts = TenantSession(session, tenant_id)
            created = await ensure_subscription(ts, plan_id=plan_id)
            if created is None:
                await change_plan(ts, plan_id, actor=principal.user_id)
            result["assigned"] = True

        await record_admin_action(
            session, actor=principal.user_id, action="custom_plan.create",
            target=plan_id, subject_tenant_id=tenant_id, after=result,
            note=f"derived from {body.base_plan_id}",
        )
        await session.commit()
        return result


class PlatformAdminIn(BaseModel):
    """Grant platform-admin rights to an email address."""

    model_config = {"extra": "forbid"}

    email: str = Field(min_length=3, max_length=255)
    platform_role: str = "superadmin"
    # Optional explicit override; empty means "expand the role preset".
    permissions: list[str] = Field(default_factory=list)
    note: str = ""


@router.post("/admins")
async def create_platform_admin(
    body: PlatformAdminIn,
    principal: Principal = Depends(require_platform_permission(ADMINS_MANAGE)),
) -> dict:
    """Make someone a platform admin. Only an existing platform admin can do this.

    The email does NOT have to belong to an existing user: pre-authorizing a colleague before
    they sign up is the normal onboarding order. The grant is keyed on the email, which is what
    require_platform_admin matches on.
    """
    from sqlalchemy import select

    from nexus.models.billing import PlatformAdmin

    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not an email address")
    if body.platform_role not in ("superadmin", "support", "finance"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown platform role")
    unknown = set(body.permissions) - set(ALL_PERMISSIONS)
    if unknown:
        # A typo'd permission would silently grant nothing, which is worse than a clear refusal.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown permission(s): {', '.join(sorted(unknown))}",
        )

    async with get_sessionmaker()() as session:
        row = (
            await session.scalars(select(PlatformAdmin).where(PlatformAdmin.email == email))
        ).first()
        before = snapshot(row, ("email", "platform_role", "permissions", "active"))
        created = row is None
        if row is None:
            row = PlatformAdmin(email=email)
            session.add(row)
        row.platform_role = body.platform_role
        # Store the EXPANDED set, not just the role name. If "support" is redefined tomorrow,
        # people provisioned today must not silently gain power they were never granted.
        row.permissions = (
            list(body.permissions)
            if body.permissions
            else permissions_for_role(body.platform_role)
        )
        row.active = True
        row.note = body.note
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="platform_admin.grant",
            target=email, before=before,
            after=snapshot(row, ("email", "platform_role", "permissions", "active")), note=body.note,
        )
        await session.commit()
        return {"email": email, "platform_role": row.platform_role,
                "permissions": list(row.permissions or []),
                "active": True, "created": created}


@router.delete("/admins/{email}")
async def revoke_platform_admin(
    email: str,
    principal: Principal = Depends(require_platform_permission(ADMINS_MANAGE)),
) -> dict:
    """Revoke platform-admin rights.

    Deactivates rather than deletes, so the audit trail keeps pointing at a real row. Refuses to
    remove the last active admin: locking every operator out of the billing console is not a
    state anyone can recover from through the product.
    """
    from sqlalchemy import func, select

    from nexus.models.billing import PlatformAdmin

    email = email.strip().lower()
    async with get_sessionmaker()() as session:
        row = (
            await session.scalars(select(PlatformAdmin).where(PlatformAdmin.email == email))
        ).first()
        if row is None or not row.active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{email} is not an active admin")

        remaining = await session.scalar(
            select(func.count(PlatformAdmin.id)).where(
                PlatformAdmin.active == True  # noqa: E712
            )
        )
        if int(remaining or 0) <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot revoke the last active platform admin; grant another one first",
            )

        before = snapshot(row, ("email", "platform_role", "permissions", "active"))
        row.active = False
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="platform_admin.revoke",
            target=email, before=before,
            after=snapshot(row, ("email", "platform_role", "active")),
        )
        await session.commit()
        return {"email": email, "active": False}


# ---- subscription CRUD ---------------------------------------------------------------------------
# Create and change already existed (`POST .../subscription`), as did pause and resume. Cancel had a
# service function and no endpoint at all, so the one lifecycle step a support admin most often has
# to perform was the one they could not — and the workaround, moving the customer to `free`, keeps
# billing them nothing while leaving the subscription "active", which reads as a live customer in
# every report.


class SubscriptionPatchIn(BaseModel):
    """Fine-grained edits. Every field is optional; only what is sent is changed.

    `plan_id` is deliberately NOT here — changing a plan runs proration, which is arithmetic with
    consequences, and it has its own endpoint. A PATCH that silently repriced a customer because a
    form posted every field it had loaded is the accident this separation prevents.
    """

    model_config = {"extra": "forbid"}

    status: str | None = None
    trial_end: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool | None = None
    seats_included: int | None = None
    grandfathered: bool | None = None
    reason: str = ""


class CancelIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Default True: the customer paid through the period, so ending access immediately takes back
    # something they bought. Immediate cancellation is available, but it has to be asked for.
    at_period_end: bool = True
    reason: str = ""


@router.get("/tenants/{tenant_id}/subscription")
async def get_tenant_subscription(
    tenant_id: str,
    _: Principal = Depends(require_platform_permission(BILLING_READ)),
) -> dict:
    """One workspace's subscription in full, including the fields the list view omits.

    Read on the PLATFORM sessionmaker: `billing_subscriptions` is tenant-scoped, so the app role
    would return no row and the endpoint would report "no subscription" for a paying customer.
    """
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription

    async with get_platform_sessionmaker()() as session:
        sub = (await session.scalars(
            select(BillingSubscription).where(BillingSubscription.tenant_id == tenant_id)
        )).first()
        if sub is None:
            # Not a 404: the workspace may exist and simply have no subscription, which is a real
            # and actionable state — it is exactly who an operator is about to put on a plan.
            return {"tenant_id": tenant_id, "subscription": None}
        plan = await session.get(BillingPlan, sub.plan_id) if sub.plan_id else None
        return {
            "tenant_id": tenant_id,
            "subscription": {
                "id": sub.id,
                "plan_id": sub.plan_id,
                "plan_name": (plan.name if plan is not None else sub.plan_id) or "-",
                "plan_class": getattr(plan, "plan_class", "") if plan is not None else "",
                "status": sub.status,
                "interval": sub.interval,
                "currency": sub.currency,
                "current_period_start": sub.current_period_start.isoformat()
                if sub.current_period_start else None,
                "current_period_end": sub.current_period_end.isoformat()
                if sub.current_period_end else None,
                "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
                "cancel_at_period_end": bool(sub.cancel_at_period_end),
                "grandfathered": bool(sub.grandfathered),
                "seats_included": sub.seats_included,
                # Whether this deal has a provider object at all. An enterprise contract never had
                # one, and reconciliation skips it for that reason — the UI has to be able to say
                # "this is not a Stripe subscription" rather than "Stripe is missing it".
                "psp_customer_id": sub.psp_customer_id or "",
                "psp_subscription_id": sub.psp_subscription_id or "",
            },
        }


@router.patch("/tenants/{tenant_id}/subscription")
async def patch_tenant_subscription(
    tenant_id: str,
    body: SubscriptionPatchIn,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """Edit the terms without changing the plan.

    Extending a trial, correcting a period end after a support conversation, adjusting included
    seats for a bespoke deal: all real operator tasks that previously needed a database session.

    `status` is validated against `SUBSCRIPTION_STATUSES` because rating and entitlements switch on
    it — a value outside that vocabulary is not a stricter setting, it is a subscription neither
    system can reason about.
    """
    from nexus.models.billing import SUBSCRIPTION_STATUSES, BillingSubscription

    fields = body.model_dump(exclude_unset=True, exclude={"reason"})
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to change")
    if "status" in fields and fields["status"] not in SUBSCRIPTION_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"status must be one of {SUBSCRIPTION_STATUSES}",
        )
    if "seats_included" in fields and fields["seats_included"] is not None \
            and int(fields["seats_included"]) < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "seats_included cannot be negative")

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        sub = await ts.first(BillingSubscription)
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "this workspace has no subscription to edit")
        before = {k: getattr(sub, k) for k in fields}
        for key, value in fields.items():
            setattr(sub, key, value)
        await ts.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.patch",
            target=sub.plan_id, subject_tenant_id=tenant_id,
            # Datetimes do not survive the audit's JSON column; str() keeps the before/after
            # readable rather than dropping the very fields most likely to be disputed later.
            before={k: str(v) for k, v in before.items()},
            after={k: str(v) for k, v in fields.items()},
            note=body.reason,
        )
        await session.commit()
        return {"tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status,
                "changed": sorted(fields)}


@router.post("/tenants/{tenant_id}/subscription/cancel")
async def cancel_tenant_subscription(
    tenant_id: str,
    body: CancelIn | None = None,
    principal: Principal = Depends(require_platform_permission(SUBSCRIPTIONS_WRITE)),
) -> dict:
    """Cancel. At period end by default — the customer paid through it.

    `cancel_subscription` has existed since M6 with no endpoint, so the workaround was to move the
    customer to `free`. That leaves the subscription `active` on a $0 plan, which reads as a live
    customer in revenue, in the directory, and in every count that filters on status.
    """
    from nexus.billing.subscriptions import cancel_subscription
    from nexus.models.billing import BillingSubscription

    at_period_end = body.at_period_end if body else True
    reason = (body.reason if body else "") or ""
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        existing = await ts.first(BillingSubscription)
        before = {"status": existing.status,
                  "cancel_at_period_end": bool(existing.cancel_at_period_end)} if existing else {}
        sub = await cancel_subscription(ts, at_period_end=at_period_end)
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "this workspace has no active subscription")
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.cancel",
            target=sub.plan_id, subject_tenant_id=tenant_id,
            before=before,
            after={"status": sub.status,
                   "cancel_at_period_end": bool(sub.cancel_at_period_end)},
            note=reason or ("at period end" if at_period_end else "immediate"),
        )
        await session.commit()
        return {"tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status,
                "cancel_at_period_end": bool(sub.cancel_at_period_end)}


# ---- authoring a sellable plan -------------------------------------------------------------------
# Until this existed, a ninth public tier needed a `plans.py` edit and a deploy. `CustomPlanDialog`
# could build a bespoke per-tenant deal, but a custom plan is excluded from `GET /billing/plans` and
# refused by checkout with a 409 — so there was no path from "we want to sell a new tier" to a
# customer buying it, short of shipping code.


class SellablePlanIn(BaseModel):
    # `extra="forbid"`, so a body cannot smuggle `plan_class` and mint an `unlimited` or `internal`
    # plan by typing a string. The class is decided by the service, not by the request.
    model_config = {"extra": "forbid"}

    plan_id: str
    name: str
    base_plan_id: str
    base_price_cents: int
    included_credits: int
    description: str = ""
    seat_price_cents: int | None = None
    currency: str = "USD"
    interval: str = "month"
    max_seats: int | None = None
    trial_days: int = 0
    sort_order: int | None = None
    # Draft by default: a plan is invisible to the price list until someone publishes it, so the
    # ladder cannot gain a half-configured tier the moment the form is submitted.
    status: str = "draft"
    # capability_id -> field overrides, layered on top of the cloned base. This is what makes a
    # cheaper tier cheaper: turn `module.*` off here rather than leaving it to catalog defaults,
    # which are permissive and would silently grant nearly everything.
    entitlement_overrides: dict[str, dict] = {}
    # Pay-as-you-go: every metered capability starts at quota 0, so all consumption is rated as
    # overage onto an invoice. Without it a zero-allowance plan inherits unlimited entitlements
    # and bills nothing — rating skips any capability whose quota is None.
    metered_from_zero: bool = False


class PlanStatusIn(BaseModel):
    model_config = {"extra": "forbid"}

    status: str


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_sellable_plan_endpoint(
    body: SellablePlanIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Create a plan the public price list can sell.

    Gated on `pricing.write`, the same permission that edits a rate card: this sets a price
    customers pay. Entitlements are cloned from `base_plan_id` rather than started empty, because
    `resolve_entitlement` falls back to permissive catalog defaults for anything a plan does not
    list — an empty new plan would grant nearly everything, which is the opposite of what a cheaper
    tier is for.

    Returns a `warning` when the included credits look expensive against the price. It warns rather
    than refuses: a rate card below cost is an error, but a *plan* below the cost of its own credits
    is a normal commercial decision, and a hard floor would refuse the `free` tier that already
    exists.
    """
    from nexus.billing.plan_authoring import PlanAuthoringError, create_sellable_plan

    async with get_sessionmaker()() as session:
        try:
            result = await create_sellable_plan(
                session,
                plan_id=body.plan_id,
                name=body.name,
                base_plan_id=body.base_plan_id,
                base_price_cents=body.base_price_cents,
                included_credits=body.included_credits,
                description=body.description,
                seat_price_cents=body.seat_price_cents,
                currency=body.currency,
                interval=body.interval,
                max_seats=body.max_seats,
                trial_days=body.trial_days,
                sort_order=body.sort_order,
                status=body.status,
                entitlement_overrides=body.entitlement_overrides or None,
                metered_from_zero=body.metered_from_zero,
            )
        except PlanAuthoringError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="plan.create",
            target=result["plan_id"], after=result,
        )
        await session.commit()
    return result


@router.put("/plans/{plan_id}/status")
async def set_sellable_plan_status(
    plan_id: str,
    body: PlanStatusIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Publish a plan to the price list, or hold it.

    `draft` is the hold: the plan leaves the price list and existing subscribers are untouched,
    because entitlements resolve from the plan row rather than from what is currently on sale. That
    is the difference between holding and retiring, and it is the one an operator wants when a price
    is wrong while customers are mid-purchase.
    """
    from nexus.billing.plan_authoring import PlanAuthoringError, set_plan_status

    async with get_sessionmaker()() as session:
        before = await session.get(BillingPlan, plan_id)
        was = before.status if before is not None else None
        try:
            plan = await set_plan_status(session, plan_id, body.status)
        except PlanAuthoringError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="plan.status",
            target=plan_id, before={"status": was}, after={"status": plan.status},
        )
        await session.commit()
        return {"plan_id": plan_id, "status": plan.status,
                "sellable": plan.status == "active"}
