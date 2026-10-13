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

import logging

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

logger = logging.getLogger("nexus.api.billing")

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
    # True when the workspace pays through a live Stripe subscription: its price list switches plan
    # on that subscription (`/billing/switch-plan`) instead of opening a second Checkout.
    switch_in_place: bool = False


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
        switch_in_place=bool(sub is not None and sub.status in ("trialing", "active", "past_due")
                             and (sub.psp_subscription_id or "").strip()),
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

# Classes that cannot be bought through hosted checkout. `free` is there to be seen, not
# purchased: a $0 subscription is a downgrade, and routing it through a payment page would create
# a Stripe product for a plan that never charges anyone.
#
# The other four are the same argument with a much sharper edge, and they were missing. Found by
# probing the running deployment: a customer POSTed `plan_id: "internal"` and received a real
# Stripe Checkout session. `internal` is the STAFF tier — $0, and its class is in
# `entitlements._UNLIMITED_CLASSES`, so `resolve_entitlement` returns `mode="unlimited"` and every
# quota check and every credit burn is skipped. Completing that $0 checkout puts a customer on
# unlimited everything. `unlimited` (the pre-billing grandfather plan) and `partner` grant the same
# thing; `trial` is $0 with credits attached and is meant to be granted by the trial flow.
#
# `test_checkout_plan_gating.py` binds this to `_UNLIMITED_CLASSES` rather than to a copy of it,
# so a fifth unlimited class cannot quietly open a fifth way to buy unlimited usage.
UNPURCHASABLE_PLAN_CLASSES = ("free", "internal", "unlimited", "partner", "trial")


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


def _own_url(value: str, default_suffix: str) -> str:
    """Where Stripe sends the customer back: this app, or a 422 (2026-10-12).

    The address came from the request body and went to Stripe unchecked, so anyone with a
    workspace could mint a genuine Stripe page that ends on a site of their choosing: an open
    redirect behind a payment page. A path is taken relative to `app_base_url`; a full address
    must be on that origin. With no `app_base_url` (local) there is nothing to compare against
    and the value passes as before; production refuses to take money without one anyway, since
    Stripe needs absolute return addresses.
    """
    from urllib.parse import urlsplit

    from nexus.core.config import get_settings

    if not value:
        return _default_url(default_suffix)
    base = (get_settings().app_base_url or "").rstrip("/")
    if not base:
        return value
    refused = HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Return addresses must be on this app.")
    # Backslashes, spaces and control characters: browsers read "/\evil.example" as
    # "//evil.example", and nothing legitimate needs any of them.
    if any(ch == "\\" or ord(ch) <= 0x20 for ch in value):
        raise refused
    if value.startswith("/") and not value.startswith("//"):
        return f"{base}{value}"
    ours, theirs = urlsplit(base), urlsplit(value)
    if (theirs.scheme, theirs.netloc.lower()) != (ours.scheme, ours.netloc.lower()):
        raise refused
    return value


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
    # The tier this row belongs to (shared by its monthly and annual rows) and the same tier on the
    # other interval, or None when it is sold on one only. Decided here by `interval_pairs`, so the
    # picker renders one card per tier without guessing pairs from ids or names.
    family: str
    counterpart_id: str | None


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
    from nexus.billing.plans import interval_pairs
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
    # A superadmin chooses which of these the list shows, at most four cards (2026-10-09). A hidden
    # plan is still on sale; it is only left off this list.
    from nexus.billing.price_list import is_shown

    rows = [p for p in rows if is_shown(p)]

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

    # Over the listed rows only, so a held monthly tier cannot pair an annual with a plan checkout
    # would refuse.
    pairs = interval_pairs(rows)

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
                family=pairs[plan.id].family, counterpart_id=pairs[plan.id].counterpart_id,
            )
        )
    return out


async def _refuse_if_checkout_cannot_take_payment(provider, currency: str) -> None:
    """A 409 with a sentence, instead of sending the customer to a Stripe page that says "There are
    no valid payment methods available" (reported 2026-10-12). Only for what definitely stops
    payment (`checkout_readiness` blocking causes); the reason goes to the log and to Platform
    health, where the person who can fix it looks. A check that cannot run lets checkout go ahead:
    a failing diagnostic must not stop sales."""
    from nexus.billing import checkout_readiness

    try:
        ready = await checkout_readiness.checkout_readiness(provider, currencies={currency})
    except Exception:
        logger.warning("checkout readiness could not be read; continuing", exc_info=True)
        return
    # The account's own facts, on every Checkout opened: when Stripe's page then says "no valid
    # payment methods", the app log is where an operator looks, and it said only "200" (2026-10-13).
    logger.info("checkout: stripe account %s country=%s currency=%s card_payments=%s "
                "charges_enabled=%s", ready.account_id or "?", ready.country or "?", currency,
                ready.card_payments or "?", ready.charges_enabled)
    for warning in ready.warnings:
        logger.warning("checkout opened with a warning: %s", warning)
    if ready.blocking:
        logger.warning("checkout refused, the Stripe account cannot take payment: %s",
                       " ".join(ready.blocking))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Payments cannot be taken right now, so checkout did not open. Nothing was charged. "
            "The platform team has been alerted; please try again later or contact support.",
        )


async def _refuse_a_second_subscription(ts: TenantSession) -> None:
    """A workspace already paying through Stripe gets a sentence, not a second subscription.

    Found 2026-10-12: a workspace on Launch pressed "Continue to checkout" on Accelerate, Checkout
    created a SECOND Stripe subscription, our row moved to it, and Stripe billed both. A plan change
    for a paying workspace happens on its existing subscription (`POST /billing/switch-plan`)."""
    from nexus.billing.stripe_subscriptions import stripe_subscription_of

    billed = await stripe_subscription_of(ts)
    if billed is None:
        return
    current = await ts.session.get(BillingPlan, billed.plan_id)
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"This workspace already pays for {current.name if current else billed.plan_id} through "
        "Stripe, so a second checkout would charge it twice. Nothing was charged. To change plan, "
        "choose Switch on the plan you want.",
    )


async def _expire_the_page_opened_before(provider, track: BillingSubscription) -> None:
    """Only the newest Checkout page for a workspace can be paid (2026-10-12).

    Two tabs, or a page left open, are two pages that can each start a subscription. Expiring the
    previous one first means at most one is payable. If the customer had already paid on it, its
    subscription is on its way through the webhook and buying again would double it, so that is a
    409. A failure to expire is logged and does not stop the sale: the webhook still refuses to
    follow two subscriptions."""
    import time as _time

    opened = (track.meta or {}).get("open_checkout") or {}
    session_id = str(opened.get("id") or "")
    if not session_id or int(opened.get("expires_at") or 0) <= _time.time():
        return
    try:
        state = await provider.expire_checkout_session(session_id)
    except Exception:
        logger.warning("could not expire checkout session %s; continuing", session_id,
                       exc_info=True)
        return
    if state == "complete":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A checkout for this workspace has just completed, and its plan arrives within a "
            "minute. Refresh this page before buying again. Nothing more was charged.",
        )


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

    success_url = _own_url(body.success_url, "/settings/billing?checkout=success")
    cancel_url = _own_url(body.cancel_url, "/settings/billing?checkout=cancelled")

    sub = await _current_subscription(ts)
    await _reject_if_admin_managed(ts, sub)
    await _refuse_a_second_subscription(ts)

    # Every checkout for this workspace runs one at a time (a row lock; SQLite ignores it), so two
    # requests cannot both find no open page and open one each.
    from nexus.billing.subscriptions import _any_subscription

    track = sub or await _any_subscription(ts)
    if track is not None:
        await ts.session.execute(
            ts.select(BillingSubscription, BillingSubscription.id == track.id).with_for_update())

    provider = await resolve_payment_provider()
    await _refuse_if_checkout_cannot_take_payment(provider, plan.currency)
    if track is not None:
        await _expire_the_page_opened_before(provider, track)
    try:
        # The price object has to exist at the PSP before a Checkout line item can reference it.
        #
        # The cached price is reused only while the plan's price terms are the ones it was made
        # for (a price changed on the Plans tab kept charging the OLD amount at Stripe, found
        # 2026-10-02) AND only inside the Stripe account it was made in. Both the price and the
        # customer were cached without their account, so after the key moved to another account
        # (test to live, or a new test account) checkout sent ids Stripe could not find: "No such
        # price" (reported 2026-10-09). `stripe_catalog` owns that rule for every caller.
        from nexus.billing.stripe_catalog import current_account, customer_id_for, plan_price_id

        account = await current_account(provider)
        price_id = await plan_price_id(plan, provider, account=account)
        # billing_plans is platform-global (no tenant_id, no RLS policy), so caching the
        # reference here is a plain write, not a cross-tenant one.
        await ts.session.flush()

        email = await _billing_email(principal.user_id)
        customer_id = await customer_id_for(
            sub, provider, tenant_id=ts.tenant_id, email=email, account=account,
        )
        if sub is not None:
            await ts.flush()

        session_out = await provider.create_checkout_session(
            tenant_id=ts.tenant_id, plan_id=plan.id, price_id=price_id,
            customer_id=customer_id, success_url=success_url, cancel_url=cancel_url,
        )
        if track is not None and session_out.get("id"):
            track.meta = {**(track.meta or {}), "open_checkout": {
                "id": str(session_out["id"]), "plan_id": plan.id,
                "expires_at": int(session_out.get("expires_at") or 0)}}
            await ts.flush()
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

    return_url = _own_url(body.return_url, "/settings/billing")
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
    from nexus.billing.stripe_catalog import customer_id_for
    from nexus.billing.stripe_subscriptions import owner_email

    try:
        # After a Stripe account switch the cached customer belongs to the old account. The portal
        # opens on this workspace's customer in the account billing NOW, found or carried over by
        # the rule checkout uses (2026-10-09).
        customer_id = await customer_id_for(
            sub, provider, tenant_id=ts.tenant_id, email=await owner_email(ts)) or customer_id
    except PaymentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"payment provider: {exc}") from exc
    await ts.flush()
    try:
        session_out = await provider.create_billing_portal_session(
            customer_id=customer_id, return_url=return_url,
        )
    except PaymentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"payment provider: {exc}") from exc

    return HostedSessionOut(
        id=str(session_out.get("id", "")), url=str(session_out.get("url", "")),
        provider=str(session_out.get("provider", provider.name)),
    )


class SwitchPlanIn(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str


class SwitchPlanOut(BaseModel):
    #: `switched`: on the new plan now. `confirm_payment`: the bank wants the customer to confirm
    #: the payment on Stripe's invoice page; the plan changes once they do.
    status: str
    plan_id: str
    charged_cents: int = 0
    currency: str = "USD"
    invoice_id: str = ""
    hosted_invoice_url: str = ""
    period_end: str | None = None


@router.post("/switch-plan", response_model=SwitchPlanOut)
async def switch_plan(
    body: SwitchPlanIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> SwitchPlanOut:
    """Change plan on the Stripe subscription this workspace already pays (2026-10-12).

    Checkout refuses a workspace that pays through Stripe, because a second Checkout made a second
    subscription and Stripe billed both. The new plan starts today and is charged in full now, the
    rest of the current period is not refunded and the credits already held stay: every credit
    handed out is matched by a payment, which a prorated switch with credits granted up front would
    not be (upgrade on the last day for a few cents, take the whole allowance). It applies only if
    the payment succeeds. Our row moves only after Stripe agreed, under a row lock the webhook
    also takes, so its credits cannot be granted twice.
    """
    from nexus.billing.credits import grant_credits
    from nexus.billing.payments import PaymentError, PaymentNotConfigured, resolve_payment_provider
    from nexus.billing.rollups import period_key
    from nexus.billing.stripe_subscriptions import (
        NO_CREDENTIAL,
        in_previous_account,
        stripe_subscription_of,
    )
    from nexus.billing.subscriptions import take_plan_terms
    from nexus.billing.webhooks import _epoch
    from nexus.core.db import utcnow

    plan = await ts.session.get(BillingPlan, body.plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown plan '{body.plan_id}'")
    billed = await stripe_subscription_of(ts)
    if billed is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This workspace does not pay through Stripe yet, so there is nothing to switch. "
            "Choose Continue to checkout on the plan you want.")
    await _reject_if_admin_managed(ts, billed)
    if plan.status != "active" or plan.plan_class in ADMIN_MANAGED_PLAN_CLASSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"'{plan.name}' is not on sale.")
    if plan.plan_class in UNPURCHASABLE_PLAN_CLASSES or int(plan.base_price_cents or 0) <= 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"To move to {plan.name}, cancel your plan with Manage payment method. It moves to "
            f"{plan.name} when the period you paid for ends.")
    if billed.plan_id == plan.id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This workspace is already on {plan.name}.")
    current = await ts.session.get(BillingPlan, billed.plan_id)
    if billed.status == "past_due":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A payment for this workspace is overdue. Update your card with Manage payment "
            "method, then switch.")
    if billed.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Plans can be switched once the trial has ended. Nothing was changed.")
    if billed.cancel_at_period_end:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your plan is set to end. Renew it with Manage payment method first, then switch.")
    if str((billed.meta or {}).get("collection") or "") == "send_invoice":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This workspace is billed by invoice. Ask your account team to change its plan.")
    if current is not None and (current.currency or "USD") != (plan.currency or "USD"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{plan.name} is sold in {plan.currency}, and this workspace pays in "
            f"{current.currency}.")

    # One switch at a time per workspace, and the webhook waits for it (row lock; SQLite ignores).
    await ts.session.execute(
        ts.select(BillingSubscription, BillingSubscription.id == billed.id).with_for_update())

    psp = (billed.psp_subscription_id or "").strip()
    provider = await resolve_payment_provider()
    if provider.name != "stripe" and psp.startswith("sub_"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This workspace is billed through Stripe. {NO_CREDENTIAL}")
    try:
        from nexus.billing.stripe_catalog import current_account, plan_price_id

        if provider.name == "stripe":
            from nexus.billing.stripe_carryover import ensure_in_current_account

            moved = await ensure_in_current_account(ts.session, billed, provider,
                                                    actor=principal.user_id)
            if moved["outcome"] in ("failed", "skipped"):
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    str(moved.get("detail") or "It cannot be switched now."))
            psp = (billed.psp_subscription_id or "").strip()
        price_id = await plan_price_id(plan, provider, account=await current_account(provider))
        await ts.session.flush()
        started = billed.current_period_start
        anchor = int(started.timestamp()) if started is not None else 0
        result = await provider.switch_subscription_now(
            subscription_id=psp, price_id=price_id, plan_id=plan.id,
            idempotency_key=f"switch:{psp}:{plan.id}:{anchor}")
    except PaymentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PaymentError as exc:
        gone = in_previous_account(exc, psp)
        raise HTTPException(
            status.HTTP_409_CONFLICT if gone else status.HTTP_502_BAD_GATEWAY,
            gone or f"Stripe refused the switch, so nothing changed: {exc}") from exc

    charged = result.get("amount_paid")
    charged_cents = int(charged) if isinstance(charged, int) else int(plan.base_price_cents or 0)
    out = SwitchPlanOut(status="switched", plan_id=plan.id, charged_cents=charged_cents,
                        currency=plan.currency or "USD",
                        invoice_id=str(result.get("invoice_id") or ""),
                        hosted_invoice_url=str(result.get("hosted_invoice_url") or ""))
    if not result.get("applied"):
        if result.get("needs_action") and out.hosted_invoice_url:
            out.status = "confirm_payment"
            return out
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"{result.get('decline') or 'The payment was refused.'} Nothing changed. Update your "
            "card with Manage payment method and try again.")

    previous = billed.plan_id
    take_plan_terms(billed, plan)
    start, end = _epoch(result.get("current_period_start")), _epoch(result.get("current_period_end"))
    if start is not None:
        billed.current_period_start = start
    if end is not None:
        billed.current_period_end = end
    billed.meta = {**(billed.meta or {}), "previous_plan_id": previous,
                   "changed_by": principal.user_id, "changed_at": utcnow().isoformat(),
                   "changed_via": "self_serve_switch", "switch_invoice": out.invoice_id}
    await ts.flush()
    if plan.included_credits:
        await grant_credits(
            ts, plan.included_credits, kind="grant", reason=f"{plan.name} included credits",
            idempotency_key=f"plan_switch:{out.invoice_id or psp + ':' + plan.id + ':' + str(anchor)}",
            period_key=period_key(utcnow(), "period"))
    out.period_end = billed.current_period_end.isoformat() if billed.current_period_end else None
    logger.info("workspace %s switched %s -> %s at Stripe (%s)", ts.tenant_id, previous, plan.id,
                psp)
    return out


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
    # Menu paths a superadmin has hidden from every workspace (`nexus/features/pages.py`). Carried
    # here because the menu and the route guard already read this response; a second request would
    # be a second thing to be in flight, or failed, when the shell draws the menu. Independent of
    # billing enforcement: it is a decision about the product, not about a plan.
    hidden_pages: list[str] = []


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
    from nexus.features.pages import hidden_paths

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
        hidden_pages=await hidden_paths(),
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
    # Who spent it, resolved through this workspace's membership. "Former member" for someone who
    # has left — never the raw id, which is what the screen showed when these were missing.
    name: str = ""
    email: str = ""
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
