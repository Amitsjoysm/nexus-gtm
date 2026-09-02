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
    BillingProrationAdjustment,
    BillingSubscription,
    BillingUsageRollup,
    proration_sort_key,
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


class ProrationLineOut(BaseModel):
    kind: str
    description: str
    amount_cents: int
    days_remaining: int
    days_in_period: int


class UsageOut(BaseModel):
    plan: str | None
    plan_name: str | None
    period: str
    capabilities: list[CapabilityUsageOut]
    # Subscription state the customer needs in order to read the rest of this page: a paused
    # plan shows zero quota everywhere, and without the status that looks like a bug.
    status: str | None = None
    # Whether this workspace is on an admin-managed deal. The client needs it to decide between
    # showing a price list and saying "talk to your account team", and the alternative was sniffing
    # the plan id for a `custom-` prefix — a naming convention doing load-bearing work in the UI,
    # which breaks silently the first time a plan is named anything else.
    plan_class: str | None = None
    trial_end: str | None = None
    period_end: str | None = None
    # Mid-cycle plan changes already committed to this period's invoice. Surfaced before the
    # invoice arrives, because "why is my bill different" is the question this answers.
    pending_proration_cents: int = 0
    proration_lines: list[ProrationLineOut] = []


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
    # A live subscription wins, but a paused one still has to be shown. Selecting only the live
    # statuses sent a suspended workspace down the "no subscription" path: no plan name, no status,
    # no capabilities — a blank page that reads as a broken account rather than as a pause, and
    # gives the customer no way to find out what happened.
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    if sub is None:
        sub = next((s for s in subs if s.status == "suspended"), None)
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
    adjustments = await ts.list(
        BillingProrationAdjustment, BillingProrationAdjustment.period_key == key
    )
    return UsageOut(
        plan=sub.plan_id if sub else None,
        plan_name=plan.name if plan else None,
        period=key,
        capabilities=out,
        status=sub.status if sub else None,
        plan_class=plan.plan_class if plan else None,
        trial_end=sub.trial_end.isoformat() if sub and sub.trial_end else None,
        period_end=(
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
        pending_proration_cents=sum(a.amount_cents for a in adjustments),
        proration_lines=[
            ProrationLineOut(
                kind=a.kind, description=a.description, amount_cents=a.amount_cents,
                days_remaining=a.days_remaining, days_in_period=a.days_in_period,
            )
            for a in sorted(adjustments, key=proration_sort_key)
        ],
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
    # The provider's hosted invoice and its PDF. Empty until the invoice has been collected, and
    # empty forever on a deployment with no real payment provider — the UI shows the link only when
    # there is one, because a button that opens nothing is worse than no button.
    hosted_url: str = ""
    pdf_url: str = ""


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
            hosted_url=(inv.meta or {}).get("hosted_invoice_url", ""),
            pdf_url=(inv.meta or {}).get("invoice_pdf_url", ""),
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

# Classes that appear on the price list but cannot be bought through hosted checkout. `free` is
# there to be seen, not purchased: a $0 subscription is a downgrade, and routing it through a
# payment page would create a Stripe product for a plan that never charges anyone.
UNPURCHASABLE_PLAN_CLASSES = ("free",)


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


class SellablePlanOut(BaseModel):
    """A plan a workspace can actually switch to, with the modules it includes."""

    id: str
    name: str
    description: str
    base_price_cents: int
    currency: str
    interval: str
    included_credits: int
    max_seats: int | None
    trial_days: int
    sort_order: int
    current: bool
    # Module names, so the picker can say what the plan is rather than only what it costs.
    includes: list[str]
    excludes: list[str]


@router.get("/plans", response_model=list[SellablePlanOut])
async def list_sellable_plans(
    ts: TenantSession = Depends(get_tenant_session),
    # Same gate as /checkout and /portal: this is the money surface, and the page it feeds is
    # admin-only. A rep who hits a 402 sees their usage, not the price list.
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> list[SellablePlanOut]:
    """The price list, from the customer's side.

    `POST /billing/checkout` has always taken a `plan_id`, and there was no endpoint that said
    which ids exist — so the only way to buy a plan was to already know its id. A locked nav item
    routes here offering to "view upgrade options", which is a promise this endpoint is what keeps.

    Deliberately excluded:

    * **admin-managed classes** (`custom`, `enterprise`) — checkout refuses them with a 409, and
      listing something the next click rejects is worse than not listing it;
    * **`unlimited` and `internal`** — `legacy-unlimited` is a migration keystone and internal is
      staff-only; neither is a thing to sell, and offering a grandfathered tenant the chance to
      "upgrade" off unlimited onto a metered plan is a downgrade wearing the wrong label;
    * **`trial`** — entered by signing up, not bought.

    Non-active plans are omitted too, which is how a plan is retired: set `status` in Admin and it
    leaves the price list without any code change.
    """
    from nexus.models.billing import BillingCapability

    sub = await _current_subscription(ts)
    current_plan_id = sub.plan_id if sub is not None else None

    rows = (
        await ts.session.scalars(
            select(BillingPlan)
            # `free` is listed alongside the paid tiers: a price list that hides the free option
            # is not a price list, it is a paywall with a gap. It is excluded from CHECKOUT below
            # rather than from the list — moving to free is a downgrade, which the customer portal
            # handles, not a purchase.
            .where(
                BillingPlan.plan_class.in_(("standard", "free")),
                BillingPlan.status == "active",
            )
            .order_by(BillingPlan.sort_order)
        )
    ).all()

    modules = (
        await ts.session.scalars(
            select(BillingCapability)
            .where(BillingCapability.category == "module")
            .order_by(BillingCapability.id)
        )
    ).all()

    # Every module entitlement for every listed plan, in ONE query. Resolving these per
    # (plan x capability) is 5 x 11 = 55 round-trips to render one page, which is the N+1 this
    # endpoint would otherwise be.
    #
    # Deliberately not `resolve_entitlement`: that answers for the tenant's CURRENT subscription,
    # and the question a picker answers is "what would I get if I switched". It falls back to the
    # catalog default for an unlisted capability, and so does this — same rule, so the picker
    # cannot advertise a module the engine would refuse.
    module_ids = [c.id for c in modules]
    configured: dict[tuple[str, str], str] = {
        (e.plan_id, e.capability_id): e.mode
        for e in (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id.in_([p.id for p in rows]),
                    BillingPlanEntitlement.capability_id.in_(module_ids),
                )
            )
        ).all()
        if e.mode
    }

    out: list[SellablePlanOut] = []
    for plan in rows:
        includes: list[str] = []
        excludes: list[str] = []
        for cap in modules:
            mode = configured.get((plan.id, cap.id)) or cap.default_mode
            label = cap.name or cap.id
            # `enterprise` is "talk to us", not "you have it" — same reading as /entitlements.
            (excludes if mode in ("disabled", "enterprise") else includes).append(label)
        out.append(
            SellablePlanOut(
                id=plan.id, name=plan.name, description=plan.description or "",
                base_price_cents=plan.base_price_cents, currency=plan.currency,
                interval=plan.interval, included_credits=plan.included_credits,
                max_seats=plan.max_seats, trial_days=plan.trial_days,
                sort_order=plan.sort_order, current=plan.id == current_plan_id,
                includes=includes, excludes=excludes,
            )
        )
    return out


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
        resolve_payment_provider,
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
    if plan.plan_class in UNPURCHASABLE_PLAN_CLASSES:
        # `free` is on the price list to be SEEN, not bought. Routing a $0 plan through hosted
        # checkout would create a Stripe product for something that never charges anyone, and put a
        # card form in front of a customer who is downgrading. Moving to free is a plan change, and
        # the customer portal is where plan changes belong.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{plan.name}' is free — there is nothing to check out. Manage your plan from the "
            f"billing portal instead.",
        )

    sub = await _current_subscription(ts)
    await _reject_if_admin_managed(ts, sub)

    provider = await resolve_payment_provider()
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
        resolve_payment_provider,
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

    provider = await resolve_payment_provider()
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


# ---- entitlements: what this workspace's plan actually includes -------------------------------
#
# The sidebar was blind to entitlements, so a `free` workspace saw Network and Campaigns and
# found out by clicking and getting a 402. This endpoint is what lets navigation tell the truth.
#
# The trap it has to avoid is bigger than the bug it fixes. `NEXUS_BILLING_ENFORCEMENT` defaults
# to `shadow`, which evaluates every entitlement and then ALLOWS regardless. A UI that hid a nav
# item because the *policy* said "disabled" would hide a feature that still works perfectly —
# turning a shadow-mode rollout, whose entire promise is "changes nothing", into a visible product
# regression. So this endpoint reports the resolved policy AND `gating_active`, and the client is
# expected to gate only when the server would actually block.
#
# `gating_active` is computed here rather than left to the client to derive from `enforcement`,
# because that derivation is exactly the sort of thing two callers get subtly different.


class EntitlementOut(BaseModel):
    capability_id: str
    name: str
    mode: str
    # Whether the policy permits this capability at all. Distinct from "will the server let you
    # through right now" — see `gating_active`.
    included: bool
    # Where the answer came from (plan_class | plan | catalog | feature_flag | dependency |
    # suspended | unknown | feature_switch), so a support conversation can start from a fact.
    source: str
    # Whether the client should actually gate this item. COMPUTED HERE, not left to the client to
    # derive from `gating_active` + `included` + `switch_state`: that derivation has two readers
    # (the nav in `nav.tsx` and the route guard in `App.tsx`) and is exactly the sort of thing two
    # callers get subtly different. The rules it folds in:
    #   * a PLAN gate locks only when `gating_active` — otherwise shadow mode, whose whole promise
    #     is "changes nothing", starts hiding features the server still serves;
    #   * a PLATFORM SWITCH locks always, because it is not about billing and production runs
    #     shadow by default. Riding it on `gating_active` would make the control inert.
    locked: bool = False
    # `null` unless a platform switch is what disabled this. `disabled | coming_soon | maintenance`
    # — three sentences sharing one entitlement, and telling them apart is the difference between
    # "we are fixing this" and an upgrade prompt for a feature no plan sells.
    switch_state: str | None = None
    switch_message: str = ""


class EntitlementsOut(BaseModel):
    plan: str | None
    plan_name: str | None
    status: str | None
    enforcement: str
    # True only when the server will genuinely refuse a call. The UI must gate on THIS, never on
    # `included` alone, or shadow mode starts hiding working features.
    gating_active: bool
    modules: list[EntitlementOut]


@router.get("/entitlements", response_model=EntitlementsOut)
async def get_entitlements(
    ts: TenantSession = Depends(get_tenant_session),
    # Rep-level, like /usage: every member's navigation depends on this, and it exposes no money.
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> EntitlementsOut:
    """The module gates for this workspace, resolved through the real entitlement engine.

    Only `module.*` capabilities: navigation is coarse, and resolving the whole catalog on every
    page load would be a lot of work to answer a question about eight menu items. Per-action
    quotas stay where they belong — on the action, via `check_and_meter`.
    """
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.config import get_settings

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    sub = sub or (subs[0] if subs else None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    rows = (
        await ts.session.scalars(
            select(BillingCapability).where(BillingCapability.category == "module")
        )
    ).all()

    settings = get_settings()
    gating_active = settings.billing_enforcement == "on"

    modules: list[EntitlementOut] = []
    for cap in sorted(rows, key=lambda c: c.id):
        ent = await resolve_entitlement(ts, cap.id)
        modules.append(
            EntitlementOut(
                capability_id=cap.id,
                name=cap.name or cap.id,
                mode=ent.mode,
                # `enterprise` is "talk to us", not "you have it". Treated as not included so a
                # self-serve plan does not advertise a module it cannot actually turn on.
                included=ent.mode not in ("disabled", "enterprise"),
                source=ent.source,
                locked=(
                    ent.source == "feature_switch"
                    or (gating_active and ent.mode in ("disabled", "enterprise"))
                ),
                switch_state=ent.switch_state,
                switch_message=ent.switch_message,
            )
        )

    enforcement = settings.billing_enforcement
    return EntitlementsOut(
        plan=sub.plan_id if sub else None,
        plan_name=plan.name if plan else None,
        status=sub.status if sub else None,
        enforcement=enforcement,
        gating_active=gating_active,
        modules=modules,
    )


class CreditSpendRowOut(BaseModel):
    capability_id: str
    name: str
    credits: float
    actions: float


class CreditDayOut(BaseModel):
    date: str
    credits: float


class CreditUserRowOut(BaseModel):
    user_id: str
    credits: float


class CreditUsageReportOut(BaseModel):
    period: str
    granted: float
    spent: float
    balance: float
    by_capability: list[CreditSpendRowOut]
    by_day: list[CreditDayOut]
    by_user: list[CreditUserRowOut]
    # Spend that belongs to no user. Reported rather than hidden: background work — refresh sweeps,
    # crawls, plays — has nobody to attribute to, so `by_user` cannot sum to `spent`, and a screen
    # whose parts do not add up quietly lies about a figure the customer will check.
    unattributed_credits: float


@router.get("/usage/credits", response_model=CreditUsageReportOut)
async def get_credit_usage_report(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CreditUsageReportOut:
    """Where this workspace's credits went, three ways.

    `/billing/usage` reports per-capability ACTION COUNTS and says nothing about credits — 40
    enrichments is really 120 credits at 3 apiece, and nothing on that screen said so. On a
    credit-funded plan "where did my 2,000 go?" is the question the customer is guaranteed to ask,
    and until now the product had no answer.

    Rep-level like the rest of the read surface: the person who hits a limit is exactly the person
    who needs to see what spent it.
    """
    from nexus.billing.usage_report import credit_usage_report

    return CreditUsageReportOut(**await credit_usage_report(ts))
