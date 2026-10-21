# nexus/billing/plan_authoring.py
"""Create a plan the public price list will actually sell.

Until this existed, a ninth public tier needed a `plans.py` edit and a deploy. `CustomPlanDialog`
could build a bespoke per-tenant deal (`plan_class="custom"`), but a custom plan is deliberately
excluded from `GET /billing/plans` and refused by `/billing/checkout` with a 409 — so there was no
path from "we want to sell a new tier" to a customer buying it, short of shipping code.

The difference between the two is one field. `plan_class="standard"` is what `/billing/plans`
filters on, which is also why this module refuses to create anything else: an endpoint that could
mint an `unlimited` or `internal` plan would be a way to hand out the migration keystone or the
staff tier by typing a string.

**A new plan is `draft` unless the caller asks otherwise.** Draft plans are invisible to
`/billing/plans`, which filters on `status == "active"`, so the ladder cannot gain a half-configured
tier the moment someone hits save. Activating is a separate call, and reversible: putting a plan
back to `draft` takes it off the price list without retiring it, which is the "hold" an operator
wants when a price is wrong and customers are mid-purchase.

Entitlements are **cloned from a base plan**, never started empty. `resolve_entitlement` falls back
to each capability's catalog default when a plan does not list it, and those defaults are permissive
— so an empty new plan would silently grant nearly everything, which is the opposite of what a
cheaper tier is for.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.billing import BillingPlan, BillingPlanEntitlement

logger = logging.getLogger("nexus.billing.plan_authoring")

_ID_RE = re.compile(r"[^a-z0-9-]+")

# The only class this module will create. See the module docstring: the others are not tiers to
# sell, and several are load-bearing elsewhere.
SELLABLE_CLASS = "standard"

# `draft` hides the plan from the price list; `active` publishes it. Both are reachable on create
# and both are reachable afterwards, which is the "switch them active or hold" control.
AUTHORABLE_STATUSES = ("draft", "active")

# Reserved ids. `custom-` is the prefix `custom_plan_id` mints per tenant, and colliding with one
# would silently repoint a negotiated deal at a public tier.
_RESERVED_PREFIXES = ("custom-",)
_RESERVED_IDS = ("free", "trial", "enterprise", "internal", "legacy-unlimited")


class PlanAuthoringError(ValueError):
    """The plan cannot be created as asked."""


# ---- the annual twin (2026-10-12) ---------------------------------------------------------------
# The price list sells a tier twice, as `X` (monthly) and `X-annual` (yearly), two rows that pair by
# name (`plans.interval_pairs`) into one card with a Monthly/Annual switch. A plan made here had no
# annual unless somebody made a second plan by hand with exactly the right id, price and credits.

#: The annual discount the seeded ladder uses (Launch $99 a month -> $950 a year).
DEFAULT_ANNUAL_DISCOUNT_PCT = 20
#: Beyond this an "annual discount" is a giveaway typed by mistake.
MAX_ANNUAL_DISCOUNT_PCT = 90


def annual_price_cents(monthly_cents: int, discount_pct: int) -> int:
    """Twelve months less the discount, to the nearest whole unit, halves up.

    Integer arithmetic, so it is exact and identical to the preview in `PlanDialog.tsx`, which uses
    the same expression: a dialog that quotes one price and a plan made at another is the bug.
    $99 at 20% is 950.40 and becomes $950, the seeded Launch annual."""
    return ((int(monthly_cents) * 12 * (100 - int(discount_pct)) + 5000) // 10000) * 100


def implied_discount_pct(monthly_cents: int, annual_cents: int) -> int:
    """The whole % off twelve monthly payments that an annual price comes to, halves up.

    For an annual price typed in rather than derived (2026-10-21): it is what the twin records as its
    discount and what its seat price takes. A free monthly plan has nothing to discount."""
    twelve = int(monthly_cents) * 12
    if twelve <= 0:
        return 0
    return ((twelve - int(annual_cents)) * 200 + twelve) // (2 * twelve)


def _annual_twin_id(plan_id: str) -> str:
    from nexus.billing.plans import ANNUAL_ID_SUFFIX

    twin = plan_id + ANNUAL_ID_SUFFIX
    if len(twin) > 60:
        # `normalise_plan_id` cuts at 60, and a cut id no longer ends in `-annual`, so it would
        # never pair with its monthly plan.
        raise PlanAuthoringError("the plan id is too long to have an annual plan beside it")
    return twin


def normalise_plan_id(raw: str) -> str:
    """`Scale Annual` -> `scale-annual`. Ids appear in URLs, Stripe metadata and audit rows."""
    slug = _ID_RE.sub("-", (raw or "").strip().lower()).strip("-")
    if not slug:
        raise PlanAuthoringError("a plan id is required")
    return slug[:60]


async def clone_entitlements(
    session: AsyncSession, *, from_plan_id: str, to_plan_id: str,
    overrides: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """Copy one plan's entitlements onto another, then layer overrides. Returns (cloned, applied).

    Extracted from ``custom_plans.create_custom_plan`` so both authoring paths share one
    implementation: a second copy would drift, and the first thing to drift would be which fields
    get carried, which is invisible until a customer is on the wrong quota.
    """
    src_rows = (
        await session.scalars(
            select(BillingPlanEntitlement).where(
                BillingPlanEntitlement.plan_id == from_plan_id
            )
        )
    ).all()
    existing = {
        e.capability_id: e
        for e in (
            await session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == to_plan_id
                )
            )
        ).all()
    }

    cloned = 0
    for src in src_rows:
        row = existing.get(src.capability_id)
        if row is None:
            row = BillingPlanEntitlement(plan_id=to_plan_id, capability_id=src.capability_id)
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
    for capability_id, fields in (overrides or {}).items():
        row = existing.get(capability_id)
        if row is None:
            row = BillingPlanEntitlement(plan_id=to_plan_id, capability_id=capability_id)
            session.add(row)
            existing[capability_id] = row
        for field, value in fields.items():
            if hasattr(row, field):
                setattr(row, field, value)
        applied += 1

    await session.flush()
    return cloned, applied


async def margin_warning(session: AsyncSession, *, base_price_cents: int,
                         included_credits: int) -> str:
    """What this plan costs to serve if a customer burns every included credit.

    Deliberately a **warning, not a refusal**, unlike ``rates.validate_rate``. A rate card price
    below cost is an error — it loses money on every single call and nothing about the business
    model makes that intentional. A *plan* price below the cost of its own credits is a normal
    commercial decision: acquisition tiers, land-and-expand, and a free tier are all deliberately
    unprofitable. Blocking would refuse `free`, which already exists.

    Returns "" when there is nothing to say.
    """
    from nexus.models.billing import BillingCostRate, BillingRateCard

    if included_credits <= 0:
        return ""

    # Cost per credit, derived from the capabilities we actually price: the average COGS of one
    # credit across the catalog. An exact figure would need a usage mix, which a plan does not have
    # before anyone is on it.
    rates = {r.capability_id: r for r in (await session.scalars(select(BillingRateCard))).all()}
    costs = {c.capability_id: c for c in (await session.scalars(select(BillingCostRate))).all()}
    per_credit: list[float] = []
    for cap_id, rate in rates.items():
        cost = costs.get(cap_id)
        credits = float(getattr(rate, "credits_per_unit", 0) or 0)
        unit_cost = float(getattr(cost, "unit_cost_usd", 0) or 0) if cost else 0.0
        if credits > 0 and unit_cost > 0:
            per_credit.append(unit_cost / credits)
    if not per_credit:
        return ""

    avg_cost_per_credit = sum(per_credit) / len(per_credit)
    worst_case_cost_cents = avg_cost_per_credit * included_credits * 100
    if worst_case_cost_cents <= 0:
        return ""
    if base_price_cents <= 0:
        return (f"This plan is free and includes {included_credits:,} credits, which cost about "
                f"${worst_case_cost_cents / 100:,.2f} to serve if fully used.")
    margin = (base_price_cents - worst_case_cost_cents) / base_price_cents
    if margin >= 0.5:
        return ""
    return (
        f"Thin margin: {included_credits:,} credits cost about "
        f"${worst_case_cost_cents / 100:,.2f} to serve, against a "
        f"${base_price_cents / 100:,.2f} price — {margin * 100:.0f}% gross margin if a customer "
        f"burns the full allowance. Intentional for an acquisition tier; check it otherwise."
    )


async def create_sellable_plan(
    session: AsyncSession,
    *,
    plan_id: str,
    name: str,
    base_plan_id: str,
    base_price_cents: int,
    included_credits: int,
    description: str = "",
    seat_price_cents: int | None = None,
    currency: str = "USD",
    interval: str = "month",
    max_seats: int | None = None,
    trial_days: int = 0,
    sort_order: int | None = None,
    status: str = "draft",
    entitlement_overrides: dict[str, dict] | None = None,
    metered_from_zero: bool = False,
    annual_discount_pct: int | None = None,
    annual_price: int | None = None,
) -> dict:
    """Create a plan the public price list can sell. Returns a summary plus any margin warning.

    ``annual_discount_pct`` (monthly plans only) also creates ``<id>-annual`` in the same
    transaction: the same base, entitlements, seats, trial and status, priced at twelve months less
    the discount (`annual_price_cents`, seat price too), with exactly twelve months of credits (the
    discount is on price, never on credits, as the seeded ladder does), sorted beside the monthly.
    The summary carries it under ``annual``. Its id taken, or the id too long, refuses the whole
    create: half a pair is the thing this exists to prevent.

    ``annual_price`` (cents a year) sets the twin's price as typed instead (2026-10-21); the discount
    it records, and its seat price takes, is the one that price implies. It must lie between twelve
    monthly payments and the largest discount allowed. Without it nothing differs from before.

    ``metered_from_zero`` builds a **pay-as-you-go** plan: every metered entitlement gets
    ``quota=0`` so that all consumption is overage and therefore rated onto an invoice.

    That flag is not a convenience. Rating charges overage only where a quota is set — a
    capability with ``quota=None`` reads as unlimited and is skipped, contributing nothing. So a
    PAYG plan built the obvious way, by cloning a plan and setting ``included_credits=0``, would
    inherit unlimited entitlements and **bill the customer nothing at all** while metering happily.
    It would look like it was working right up to the first invoice.
    """
    plan_id = normalise_plan_id(plan_id)
    if status not in AUTHORABLE_STATUSES:
        raise PlanAuthoringError(f"status must be one of {AUTHORABLE_STATUSES}")
    if plan_id in _RESERVED_IDS or plan_id.startswith(_RESERVED_PREFIXES):
        raise PlanAuthoringError(f"'{plan_id}' is reserved")
    if base_price_cents < 0 or included_credits < 0:
        raise PlanAuthoringError("price and credits cannot be negative")
    if interval not in ("month", "year"):
        raise PlanAuthoringError("interval must be 'month' or 'year'")
    if await session.get(BillingPlan, plan_id) is not None:
        # Refused rather than updated. This endpoint creates; silently repricing a plan customers
        # are already subscribed to, because someone reused an id, is not a create.
        raise PlanAuthoringError(f"plan '{plan_id}' already exists")
    twin_id = ""
    if annual_discount_pct is not None or annual_price is not None:
        # Checked before anything is written, so a refusal leaves neither plan behind.
        if interval != "month":
            raise PlanAuthoringError("only a monthly plan can have an annual plan made beside it")
        if annual_price is not None:
            if not 0 <= int(annual_price) <= int(base_price_cents) * 12:
                raise PlanAuthoringError(
                    "the annual price must be between 0 and twelve monthly payments")
            annual_discount_pct = implied_discount_pct(base_price_cents, annual_price)
            if annual_discount_pct > MAX_ANNUAL_DISCOUNT_PCT:
                raise PlanAuthoringError(
                    f"the annual price is more than {MAX_ANNUAL_DISCOUNT_PCT}% off twelve "
                    f"monthly payments")
        if not 0 <= int(annual_discount_pct) <= MAX_ANNUAL_DISCOUNT_PCT:
            raise PlanAuthoringError(
                f"the annual discount must be between 0 and {MAX_ANNUAL_DISCOUNT_PCT}%")
        twin_id = _annual_twin_id(plan_id)
        if await session.get(BillingPlan, twin_id) is not None:
            raise PlanAuthoringError(
                f"plan '{twin_id}' already exists, so the annual plan cannot be made")

    base = await session.get(BillingPlan, base_plan_id)
    if base is None:
        raise PlanAuthoringError(f"unknown base plan '{base_plan_id}'")

    plan = BillingPlan(
        id=plan_id,
        name=name or plan_id,
        description=description or f"Derived from {base.name}",
        plan_class=SELLABLE_CLASS,
        status=status,
        base_price_cents=int(base_price_cents),
        seat_price_cents=int(
            seat_price_cents if seat_price_cents is not None else base.seat_price_cents
        ),
        currency=currency,
        interval=interval,
        included_credits=int(included_credits),
        max_seats=max_seats if max_seats is not None else base.max_seats,
        trial_days=int(trial_days),
        # Placeholder; replaced below once we can see the ladder it is joining.
        sort_order=int(sort_order) if sort_order is not None else 0,
        meta={},
    )
    if sort_order is None:
        plan.sort_order = await _position_on_ladder(session, base_price_cents, interval)
    session.add(plan)
    await session.flush()

    overrides = dict(entitlement_overrides or {})
    if metered_from_zero:
        overrides = await _zero_quota_overrides(session, overrides)

    cloned, applied = await clone_entitlements(
        session, from_plan_id=base_plan_id, to_plan_id=plan_id,
        overrides=overrides,
    )
    warning = await margin_warning(
        session, base_price_cents=base_price_cents, included_credits=included_credits,
    )

    annual = None
    if twin_id:
        pct = int(annual_discount_pct)
        annual = await create_sellable_plan(
            session,
            plan_id=twin_id,
            name=f"{plan.name} (annual)",
            base_plan_id=base_plan_id,
            base_price_cents=(int(annual_price) if annual_price is not None
                              else annual_price_cents(plan.base_price_cents, pct)),
            included_credits=plan.included_credits * 12,
            description=f"{plan.name}, paid yearly. {pct}% off.",
            seat_price_cents=annual_price_cents(plan.seat_price_cents or 0, pct),
            currency=currency,
            interval="year",
            max_seats=plan.max_seats,
            trial_days=plan.trial_days,
            sort_order=plan.sort_order + 1,
            status=status,
            entitlement_overrides=entitlement_overrides,
            metered_from_zero=metered_from_zero,
        )
        twin = await session.get(BillingPlan, twin_id)
        twin.meta = {**(twin.meta or {}), "annual_of": plan_id, "annual_discount_pct": pct}
        await session.flush()

    # **No Stripe object is created here.** A draft is not for sale, and pricing one would litter
    # the Stripe account with products for tiers that were never sold. A plan created or published
    # ON SALE is made to exist at Stripe by the endpoint (`stripe_catalog.apply_plan_to_provider`,
    # decided 2026-10-09), so a plan on the price list is always buyable.
    summary = {
        "plan_id": plan_id,
        "status": plan.status,
        "plan_class": plan.plan_class,
        "base_plan_id": base_plan_id,
        "base_price_cents": plan.base_price_cents,
        "included_credits": plan.included_credits,
        "interval": plan.interval,
        "sort_order": plan.sort_order,
        "entitlements_cloned": cloned,
        "overrides_applied": applied,
        "warning": warning,
        "sellable": plan.status == "active",
    }
    if annual is not None:
        summary["annual"] = annual
    return summary


async def _zero_quota_overrides(session: AsyncSession,
                                explicit: dict[str, dict]) -> dict[str, dict]:
    """Every priced capability starts at zero, so every unit is billable overage.

    Enumerated from the **capability catalog**, not from the base plan's entitlements. The base
    plans carry only a handful of rows each — `growth` has five — and everything else resolves
    from catalog defaults. Cloning those five and zeroing them would produce a pay-as-you-go plan
    that bills for five things and gives away the other sixty-five.

    Two exclusions, both of which would be actively wrong:

    * **Module gates** are on/off, not quantities. `quota=0` on one reads as "you may use this
      module zero times", which is not what disabling a module means.
    * **`UNPRICED_BY_DESIGN`** — chiefly `seat.member`, which is billed as a seat price rather than
      in credits. Zeroing it means *no members allowed*, so the customer cannot use the product at
      all. Caught by exactly that happening on the first build of this function.

    An explicit override still wins: a PAYG plan may want a module off, or a genuine free
    allowance on one capability as an acquisition hook.
    """
    from nexus.billing.rates import UNPRICED_BY_DESIGN
    from nexus.models.billing import BillingCapability, BillingRateCard

    caps = (await session.scalars(select(BillingCapability.id))).all()
    priced = set((await session.scalars(select(BillingRateCard.capability_id))).all())

    out: dict[str, dict] = {}
    for cap_id in caps:
        if cap_id.startswith("module.") or cap_id in UNPRICED_BY_DESIGN:
            continue
        # No rate card means nothing to rate it at, so a zero quota would only block the customer
        # rather than bill them.
        if cap_id not in priced:
            continue
        out[cap_id] = {"quota": 0, "mode": "metered"}
    out.update(explicit)
    return out


async def _position_on_ladder(session: AsyncSession, base_price_cents: int,
                              interval: str) -> int:
    """Slot the new plan between the existing tiers it out-prices and under-prices.

    The first version computed this from price alone (``10 + cents // 250``). That was wrong the
    first time it ran: the seeded ladder uses hand-picked orders (10, 15, 18, 20, 30, 40, 50, 60)
    on no particular scale, so a $149 plan scored 69 and sorted *after* $199 Business. A formula
    cannot know the spacing of a ladder it did not build — so read it instead.

    Compared **within the same interval**, because an annual price is roughly twelve monthly ones
    and comparing across the two would sort every annual plan below every monthly one.
    """
    rows = (
        await session.scalars(
            select(BillingPlan).where(
                BillingPlan.plan_class == SELLABLE_CLASS,
                BillingPlan.interval == interval,
                # ACTIVE only. A retired plan keeps its row so its subscribers keep resolving
                # entitlements, but its sort_order describes a ladder that no longer exists —
                # counting it puts new tiers in gaps between plans nobody can see. Measured: with
                # five retired tiers still in the query, a $149 plan landed on top of the $199 one.
                BillingPlan.status == "active",
            )
        )
    ).all()
    cheaper = [p for p in rows if p.base_price_cents <= base_price_cents]
    dearer = [p for p in rows if p.base_price_cents > base_price_cents]
    below = max((p.sort_order for p in cheaper), default=0)
    above = min((p.sort_order for p in dearer), default=below + 20)
    if above - below >= 2:
        return (below + above) // 2
    # No gap to sit in — take the dearer plan's slot and let it drift up. Ties sort by insertion,
    # which is stable, and an operator can set the number by hand if the order matters that much.
    return above


async def set_plan_status(session: AsyncSession, plan_id: str, status: str) -> BillingPlan:
    """Publish a plan to the price list, or take it off without retiring it.

    `draft` is the hold: the plan stops being offered and existing subscribers are untouched,
    because entitlements resolve from the plan row rather than from the price list. `retired` is
    deliberately not reachable here — it is a different decision with a different blast radius, and
    it already has a home in the plan editor.
    """
    if status not in AUTHORABLE_STATUSES:
        raise PlanAuthoringError(f"status must be one of {AUTHORABLE_STATUSES}")
    plan = await session.get(BillingPlan, plan_id)
    if plan is None:
        raise PlanAuthoringError(f"unknown plan '{plan_id}'")
    if plan.plan_class != SELLABLE_CLASS:
        # Activating an `unlimited` or `internal` plan would put the migration keystone or the
        # staff tier on the public price list.
        raise PlanAuthoringError(
            f"'{plan_id}' is a {plan.plan_class} plan; only {SELLABLE_CLASS} plans are listed"
        )
    plan.status = status
    await session.flush()
    return plan
