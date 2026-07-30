# nexus/api/routers/billing.py
"""Tenant-facing billing surface: what plan am I on, what have I used, and how do I pay?

The read surface (usage, credits, invoices) is rep-level: a rep who hits a 402 is exactly who
needs to see "17 of 20 used" (docs/billing/10-Usage-Tracking.md §2).

The two money actions — opening hosted Checkout and opening the hosted Customer Portal — are
admin-only, and they are *redirects*, not writes. Neither one changes a subscription here:
state arrives back through the webhook (``nexus/billing/webhooks.py``). Writing the
subscription ourselves at redirect time would diverge from the provider the moment the customer
abandoned the page or changed something in the portal.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
    BillingUsageRollup,
)

router = APIRouter(prefix="/billing", tags=["billing"])


class CapabilityUsageOut(BaseModel):
    capability_id: str
    name: str
    category: str
    unit: str
    used: float
    quota: int | None = None
    mode: str


class UsageOut(BaseModel):
    plan: str | None
    plan_name: str | None
    period: str
    capabilities: list[CapabilityUsageOut]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    ts: TenantSession = Depends(get_tenant_session),
    # Deliberately rep+ (manage_accounts): this returns quota counts, not money, and the rep
    # hitting a 402 is exactly who needs to see "17 of 20 used". Money surfaces are admin-only.
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> UsageOut:
    from sqlalchemy import func

    from nexus.billing.rollups import period_key, period_start
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    now = utcnow()
    key = period_key(now, "period")

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    ents: dict[str, BillingPlanEntitlement] = {}
    if sub is not None:
        for e in (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id
                )
            )
        ).all():
            ents[e.capability_id] = e

    rollups = {
        r.capability_id: float(r.quantity)
        for r in (
            await ts.session.scalars(
                ts.select(
                    BillingUsageRollup,
                    BillingUsageRollup.period_kind == "period",
                    BillingUsageRollup.period_key == key,
                )
            )
        ).all()
    }

    # The rollup alone lags reality between sweeps. Enforcement counts rollup + unrolled tail,
    # so this must too, or a rep sees "17 of 20" while a 402 tells them they are at 20. One
    # grouped query keeps that agreement without a per-capability N+1.
    tail = {
        cap: float(qty or 0)
        for cap, qty in (
            await ts.session.execute(
                select(
                    BillingUsageEvent.capability_id,
                    func.sum(BillingUsageEvent.quantity),
                )
                .where(
                    BillingUsageEvent.tenant_id == ts.tenant_id,
                    BillingUsageEvent.rolled_at.is_(None),
                    BillingUsageEvent.occurred_at >= period_start(now),
                )
                .group_by(BillingUsageEvent.capability_id)
            )
        ).all()
    }

    caps = (
        await ts.session.scalars(
            select(BillingCapability)
            .where(BillingCapability.active == True)  # noqa: E712
            .order_by(BillingCapability.category, BillingCapability.id)
        )
    ).all()

    out = []
    for c in caps:
        ent = ents.get(c.id)
        used = rollups.get(c.id, 0.0) + tail.get(c.id, 0.0)
        # Only surface things the customer can actually reason about: anything they've used, or
        # anything their plan puts a number on. Pure-internal shadow meters stay hidden.
        if used == 0 and ent is None:
            continue
        out.append(
            CapabilityUsageOut(
                capability_id=c.id, name=c.name, category=c.category, unit=c.unit,
                used=used, quota=ent.quota if ent else None,
                mode=ent.mode if ent else c.default_mode,
            )
        )
    return UsageOut(
        plan=sub.plan_id if sub else None,
        plan_name=plan.name if plan else None,
        period=key,
        capabilities=out,
    )


class CreditEntryOut(BaseModel):
    id: str
    delta: float
    kind: str
    reason: str
    created_at: str


class CreditsOut(BaseModel):
    balance: float
    entries: list[CreditEntryOut]


class InvoiceLineOut(BaseModel):
    kind: str
    capability_id: str | None
    description: str
    quantity: float
    amount_cents: int


class InvoiceOut(BaseModel):
    id: str
    number: str
    period_key: str
    status: str
    currency: str
    total_cents: int
    finalized_at: str | None
    lines: list[InvoiceLineOut]


@router.get("/credits", response_model=CreditsOut)
async def get_credits(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CreditsOut:
    """Credit balance plus recent movements.

    The balance is SUM(delta) over an append-only ledger, never a stored counter, so what the
    customer sees is derived from the same rows an audit would read.
    """
    from nexus.billing.credits import balance, history

    entries = await history(ts, limit=50)
    return CreditsOut(
        balance=await balance(ts),
        entries=[
            CreditEntryOut(
                id=e.id, delta=float(e.delta), kind=e.kind, reason=e.reason,
                created_at=e.created_at.isoformat() if e.created_at else "",
            )
            for e in entries
        ],
    )


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[InvoiceOut]:
    """This workspace's invoices, newest period first, each with its charge lines."""
    from nexus.models.billing import BillingInvoice, BillingInvoiceLine

    invoices = await ts.list(BillingInvoice)
    invoices.sort(key=lambda i: i.period_key, reverse=True)
    all_lines = await ts.list(BillingInvoiceLine)
    by_invoice: dict[str, list] = {}
    for line in all_lines:
        by_invoice.setdefault(line.invoice_id, []).append(line)

    return [
        InvoiceOut(
            id=inv.id, number=inv.number, period_key=inv.period_key, status=inv.status,
            currency=inv.currency, total_cents=inv.total_cents,
            finalized_at=inv.finalized_at.isoformat() if inv.finalized_at else None,
            lines=[
                InvoiceLineOut(
                    kind=ln.kind, capability_id=ln.capability_id, description=ln.description,
                    quantity=float(ln.quantity), amount_cents=ln.amount_cents,
                )
                for ln in sorted(by_invoice.get(inv.id, []), key=lambda x: (x.kind, x.description))
            ],
        )
        for inv in invoices
    ]


# ---- self-serve money actions ----------------------------------------------------------------
# Admin+ (manage_workspace), not the rep-level read surface above: these open a page where
# somebody's card gets charged.

# Plan classes that are never self-serve. A custom/enterprise deal is negotiated and managed by
# a platform admin (nexus/billing/custom_plans.py); routing one through hosted Checkout would
# let a tenant admin re-buy their own bespoke contract at whatever the price row happens to say.
ADMIN_MANAGED_PLAN_CLASSES = ("custom", "enterprise")


class CheckoutRequest(BaseModel):
    plan_id: str
    success_url: str = ""
    cancel_url: str = ""


class PortalRequest(BaseModel):
    return_url: str = ""


class HostedSessionOut(BaseModel):
    id: str
    url: str
    provider: str
    plan_id: str | None = None


def _default_url(suffix: str) -> str:
    from nexus.core.config import get_settings

    base = (get_settings().app_base_url or "").rstrip("/")
    return f"{base}{suffix}" if base else suffix


async def _current_subscription(ts: TenantSession) -> BillingSubscription | None:
    from nexus.billing.subscriptions import ACTIVE_STATUSES

    subs = await ts.list(BillingSubscription, limit=5)
    return next((s for s in subs if s.status in ACTIVE_STATUSES), None)


async def _reject_if_admin_managed(ts: TenantSession, sub: BillingSubscription | None) -> None:
    """409 if this workspace is on an admin-managed deal.

    Deliberately a hard refusal rather than a silent redirect: an enterprise customer clicking
    "manage billing" and landing in a self-serve portal that knows nothing about their contract
    is worse than being told to talk to their account team.
    """
    if sub is None:
        return
    plan = await ts.session.get(BillingPlan, sub.plan_id)
    if plan is not None and plan.plan_class in ADMIN_MANAGED_PLAN_CLASSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This workspace is on the admin-managed plan '{plan.name}'. "
            "Contact your account team to change it; self-serve billing is disabled.",
        )


async def _billing_email(user_id: str) -> str:
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        return (user.email or "") if user is not None else ""


@router.post("/checkout", response_model=HostedSessionOut)
async def create_checkout(
    body: CheckoutRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> HostedSessionOut:
    """Open hosted Checkout for a self-serve plan and hand back the redirect URL.

    Nothing about the subscription is written here. ``checkout.session.completed`` and the
    ``customer.subscription.*`` events that follow are what move our database, so an abandoned
    Checkout leaves no half-subscribed tenant behind.
    """
    from nexus.billing.payments import (
        PaymentError,
        PaymentNotConfigured,
        get_payment_provider,
    )

    plan = await ts.session.get(BillingPlan, body.plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown plan '{body.plan_id}'")
    if plan.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"plan '{plan.id}' is {plan.status}, not on sale"
        )
    if plan.plan_class in ADMIN_MANAGED_PLAN_CLASSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{plan.name}' is an admin-managed plan and cannot be bought self-serve.",
        )

    sub = await _current_subscription(ts)
    await _reject_if_admin_managed(ts, sub)

    provider = get_payment_provider()
    try:
        # The price object has to exist at the PSP before a Checkout line item can reference it.
        # ensure_plan_price is keyed on plan id + amount, so this is a lookup after the first
        # call rather than a new price each time.
        price_id = str((plan.meta or {}).get("price_id") or "")
        if not price_id:
            refs = await provider.ensure_plan_price(
                plan_id=plan.id, name=plan.name, amount_cents=plan.base_price_cents,
                currency=plan.currency, interval=plan.interval,
            )
            price_id = str(refs.get("price_id") or "")
            # billing_plans is platform-global (no tenant_id, no RLS policy), so caching the
            # reference here is a plain write, not a cross-tenant one.
            plan.meta = {**(plan.meta or {}), **refs}
            await ts.session.flush()

        customer_id = (sub.psp_customer_id or "") if sub is not None else ""
        if not customer_id:
            email = await _billing_email(principal.user_id)
            if email:
                customer_id = await provider.ensure_customer(
                    tenant_id=ts.tenant_id, email=email
                )
                if sub is not None:
                    sub.psp_customer_id = customer_id
                    await ts.flush()

        session_out = await provider.create_checkout_session(
            tenant_id=ts.tenant_id, plan_id=plan.id, price_id=price_id,
            customer_id=customer_id,
            success_url=body.success_url or _default_url("/settings/billing?checkout=success"),
            cancel_url=body.cancel_url or _default_url("/settings/billing?checkout=cancelled"),
        )
    except PaymentNotConfigured as exc:
        # 503, not 500: the deployment is missing a key, and saying so plainly is what lets an
        # operator fix it. There is nothing the caller can retry.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"payment provider: {exc}") from exc

    return HostedSessionOut(
        id=str(session_out.get("id", "")), url=str(session_out.get("url", "")),
        provider=str(session_out.get("provider", provider.name)), plan_id=plan.id,
    )


@router.post("/portal", response_model=HostedSessionOut)
async def create_portal(
    body: PortalRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> HostedSessionOut:
    """Open the provider's hosted self-service portal (cards, cancellation, invoice history)."""
    from nexus.billing.payments import (
        PaymentError,
        PaymentNotConfigured,
        get_payment_provider,
    )

    sub = await _current_subscription(ts)
    await _reject_if_admin_managed(ts, sub)

    customer_id = (sub.psp_customer_id or "") if sub is not None else ""
    if not customer_id:
        # No PSP customer means this workspace has never paid — there is no portal to show.
        # Pointing them at Checkout is the honest answer.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This workspace has no payment account yet. Start a subscription first.",
        )

    provider = get_payment_provider()
    try:
        session_out = await provider.create_billing_portal_session(
            customer_id=customer_id,
            return_url=body.return_url or _default_url("/settings/billing"),
        )
    except PaymentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"payment provider: {exc}") from exc

    return HostedSessionOut(
        id=str(session_out.get("id", "")), url=str(session_out.get("url", "")),
        provider=str(session_out.get("provider", provider.name)),
    )
