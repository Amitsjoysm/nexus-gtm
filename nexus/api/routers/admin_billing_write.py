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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_principal, require_platform_permission
from nexus.billing.audit import record_admin_action, snapshot
from nexus.billing.permissions import ALL_PERMISSIONS, permissions_for_role, ADMINS_MANAGE, INVOICES_COLLECT, PRICING_WRITE, SUBSCRIPTIONS_WRITE
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
