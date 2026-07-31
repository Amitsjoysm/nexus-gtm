# nexus/billing/revenue.py
"""Revenue reporting: MRR, ARR, expansion, contraction, churn, LTV.

``grep -riE "\\bmrr\\b|churn|ltv"`` returned nothing across the codebase. A commercial platform that
cannot state its own recurring revenue is not finished, however correct its metering is.

**Derived, never stored.** Every number here is computed from `billing_subscriptions`,
`billing_plans` and `billing_invoices` at read time. No new table, no new write, no scheduled job to
fall behind. That matters more than the query cost: a stored MRR figure is a second source of truth
that drifts from the subscriptions it claims to describe, and reconciling the two becomes somebody's
month-end job forever.

**Cross-tenant by construction.** These are *platform* metrics — the operator's view of the whole
business — so they run through the platform sessionmaker (owner role), never a `TenantSession`.
Under the app's RLS-bound role a cross-tenant aggregate silently returns zero rows rather than
erroring, which would report an MRR of $0 and look like a catastrophe rather than a bug. That trap
is documented in CLAUDE.md and has bitten this project before.

Definitions are the conventional SaaS ones and are stated explicitly, because "MRR" means slightly
different things at different companies and a number nobody can define is a number nobody trusts:

* **MRR** — the sum of normalised monthly recurring plan revenue for subscriptions in a *live*
  state. Annual plans are divided by 12. Usage overage is **not** included: it is real revenue but
  it is not recurring, and folding it in makes MRR jump with a heavy month.
* **ARR** — MRR x 12. Not a separate measurement.
* **Expansion / contraction** — the month-over-month delta for tenants present in both periods,
  split by sign.
* **Churn** — tenants live at the start of the window and not live at the end, as a share of the
  starting count. Logo churn, not revenue churn; both are reported.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.billing.revenue")

# Subscription states that represent a paying, live customer. `past_due` counts: the debt is real
# and still owed, and dropping it would make a dunning problem look like churn.
LIVE_STATUSES = ("trialing", "active", "past_due")
# ...but a trial contributes no revenue yet, so it is live for logo counts and zero for MRR.
PAYING_STATUSES = ("active", "past_due")


@dataclass(slots=True)
class RevenueSnapshot:
    """The platform's commercial position right now."""

    mrr_cents: int = 0
    arr_cents: int = 0
    paying_tenants: int = 0
    trialing_tenants: int = 0
    past_due_tenants: int = 0
    # plan_id -> (tenant count, mrr contribution in cents)
    by_plan: dict[str, tuple[int, int]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "mrr_cents": self.mrr_cents,
            "arr_cents": self.arr_cents,
            "paying_tenants": self.paying_tenants,
            "trialing_tenants": self.trialing_tenants,
            "past_due_tenants": self.past_due_tenants,
            "by_plan": {
                plan: {"tenants": n, "mrr_cents": mrr} for plan, (n, mrr) in self.by_plan.items()
            },
        }


def monthly_cents(plan) -> int:
    """A plan's recurring revenue normalised to one month.

    Annual plans are divided by 12 rather than counted whole: otherwise a single annual customer
    makes MRR jump by twelve months of revenue in the month they sign, and fall back the next.
    Integer division keeps money in whole cents — never floats.
    """
    if plan is None:
        return 0
    base = int(plan.base_price_cents or 0)
    if (plan.interval or "month") == "year":
        return base // 12
    return base


async def snapshot() -> RevenueSnapshot:
    """Current MRR/ARR across every tenant. Never raises — a reporting failure must not take down
    the admin console that surfaces it."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan, BillingSubscription

    out = RevenueSnapshot()
    try:
        async with get_platform_sessionmaker()() as session:
            plans = {p.id: p for p in (await session.scalars(select(BillingPlan))).all()}
            subs = (
                await session.scalars(
                    select(BillingSubscription).where(
                        BillingSubscription.status.in_(LIVE_STATUSES)
                    )
                )
            ).all()

        for sub in subs:
            if sub.status == "trialing":
                out.trialing_tenants += 1
                continue          # a trial is a live logo and zero revenue
            if sub.status == "past_due":
                out.past_due_tenants += 1
            out.paying_tenants += 1
            cents = monthly_cents(plans.get(sub.plan_id))
            out.mrr_cents += cents
            count, total = out.by_plan.get(sub.plan_id, (0, 0))
            out.by_plan[sub.plan_id] = (count + 1, total + cents)
        out.arr_cents = out.mrr_cents * 12
    except Exception:
        logger.warning("revenue snapshot failed", exc_info=True)
    return out


@dataclass(slots=True)
class CollectionHealth:
    """How much of what was billed actually arrived."""

    invoiced_cents: int = 0
    paid_cents: int = 0
    outstanding_cents: int = 0
    invoices: int = 0
    paid_invoices: int = 0
    failed_invoices: int = 0

    @property
    def collection_rate(self) -> float:
        """Paid over invoiced. 1.0 when nothing was invoiced — an empty period has not failed to
        collect anything, and reporting 0% would raise an alarm about no activity at all."""
        return 1.0 if self.invoiced_cents <= 0 else self.paid_cents / self.invoiced_cents

    def as_dict(self) -> dict:
        return {
            "invoiced_cents": self.invoiced_cents,
            "paid_cents": self.paid_cents,
            "outstanding_cents": self.outstanding_cents,
            "invoices": self.invoices,
            "paid_invoices": self.paid_invoices,
            "failed_invoices": self.failed_invoices,
            "collection_rate": round(self.collection_rate, 4),
        }


async def collection_health(*, since: str = "") -> CollectionHealth:
    """Invoiced vs collected, optionally from a period key onwards.

    Draft invoices are excluded: they have not been presented to anyone, so counting them as
    uncollected would make every open period look like a collection failure.
    """
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    out = CollectionHealth()
    try:
        async with get_platform_sessionmaker()() as session:
            stmt = select(BillingInvoice).where(BillingInvoice.status != "draft")
            if since:
                stmt = stmt.where(BillingInvoice.period_key >= since)
            invoices = (await session.scalars(stmt)).all()

        for inv in invoices:
            if inv.status == "void":
                continue          # voided: never owed, so neither invoiced nor outstanding
            total = int(inv.total_cents or 0)
            out.invoices += 1
            out.invoiced_cents += total
            if inv.status == "paid":
                out.paid_invoices += 1
                out.paid_cents += total
            else:
                out.outstanding_cents += total
                if (inv.meta or {}).get("last_error"):
                    out.failed_invoices += 1
    except Exception:
        logger.warning("collection health failed", exc_info=True)
    return out


def movement(previous: dict[str, int], current: dict[str, int]) -> dict:
    """Month-over-month MRR movement between two ``{tenant_id: mrr_cents}`` maps.

    A pure function so the arithmetic is testable without a database, and so the caller decides
    what "previous" means. Categories are mutually exclusive and sum back to the net change, which
    is the property that makes the report checkable:

        net = new + expansion - contraction - churned
    """
    prev_ids, curr_ids = set(previous), set(current)
    new_ids = curr_ids - prev_ids
    churned_ids = prev_ids - curr_ids
    retained = prev_ids & curr_ids

    expansion = sum(
        max(0, current[t] - previous[t]) for t in retained
    )
    contraction = sum(
        max(0, previous[t] - current[t]) for t in retained
    )
    return {
        "new_cents": sum(current[t] for t in new_ids),
        "churned_cents": sum(previous[t] for t in churned_ids),
        "expansion_cents": expansion,
        "contraction_cents": contraction,
        "new_tenants": len(new_ids),
        "churned_tenants": len(churned_ids),
        # Logo churn as a share of the starting population. Zero when there was nobody to lose —
        # dividing by an empty base would be either a crash or a meaningless 100%.
        "logo_churn_rate": round(len(churned_ids) / len(prev_ids), 4) if prev_ids else 0.0,
        "net_cents": (
            sum(current[t] for t in new_ids)
            + expansion
            - contraction
            - sum(previous[t] for t in churned_ids)
        ),
    }
