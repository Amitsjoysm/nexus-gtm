# scripts/restructure_plans.py
"""Collapse the public ladder to Free / Launch / Accelerate. Idempotent, with a dry run.

**RETIRES the superseded tiers rather than deleting them.** `billing_subscriptions.plan_id` is a
foreign key to `billing_plans.id`, and entitlements resolve from the plan ROW rather than from
whether it is on sale. So a delete is one of two failures: a constraint violation, or a paying
customer left with no entitlements at all — which falls back to permissive catalog defaults and
quietly hands them everything.

`status="retired"` takes a plan off `GET /billing/plans` (which filters on `active`) while leaving
every existing subscriber exactly where they are. That is the difference between withdrawing a tier
and cancelling its customers.

Run the dry run first. It prints subscriber counts, and any row with subscribers is the row you
most want to be sure about.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from nexus.core.db import get_platform_sessionmaker
from nexus.models.billing import BillingPlan, BillingSubscription

# Superseded by Launch / Accelerate. `payg` and `payg-annual` go too: the new ladder leads with a
# free tier carrying 1,000 credits, which does the same job of letting someone try the product
# without committing, and does it without an invoice.
RETIRE = (
    "core", "starter", "growth", "professional", "business",
    "scale", "scale-annual", "payg", "payg-annual",
)

KEEP_ACTIVE = ("free", "launch", "launch-annual", "accelerate", "accelerate-annual")


async def main(apply: bool = False) -> None:
    async with get_platform_sessionmaker()() as session:
        plans = {p.id: p for p in (await session.scalars(select(BillingPlan))).all()}
        counts = dict(
            (
                await session.execute(
                    select(BillingSubscription.plan_id, func.count())
                    .group_by(BillingSubscription.plan_id)
                )
            ).all()
        )

        print(f"{'plan':24}{'status':16}{'subs':>6}  action")
        print("-" * 72)

        for plan_id in RETIRE:
            plan = plans.get(plan_id)
            if plan is None:
                continue
            subs = counts.get(plan_id, 0)
            if plan.status == "retired":
                action = "already retired"
            else:
                action = "RETIRE"
            keeps = "  <- keeps its subscribers, untouched" if subs else ""
            print(f"  {plan_id:22}{plan.status:16}{subs:>6}  {action}{keeps}")
            if apply and plan.status != "retired":
                plan.status = "retired"

        print()
        for plan_id in KEEP_ACTIVE:
            plan = plans.get(plan_id)
            if plan is None:
                print(f"  {plan_id:22}{'MISSING':16}{'':>6}  run sync_plans() first")
                continue
            print(f"  {plan_id:22}{plan.status:16}{counts.get(plan_id, 0):>6}  keep active")
            if apply:
                plan.status = "active"

        # `free` predates the new sizing and `sync_plans` deliberately never overwrites a live row
        # — once shipped, pricing belongs to Admin rather than to a redeploy. So it is set here.
        free = plans.get("free")
        if free is not None and free.included_credits != 1000:
            print(f"\n  free: {free.included_credits} -> 1000 credits")
            if apply:
                free.included_credits = 1000
                free.description = "Try every feature with 1,000 credits."

        # Nothing is deleted, so this can only ever be a warning — but an operator should see it.
        orphans = [p for p in counts if p not in plans]
        if orphans:
            print(f"\n  WARNING: subscriptions point at missing plans: {orphans}")

        if apply:
            await session.commit()
            print("\napplied")
        else:
            print("\ndry run - pass --apply to write")


if __name__ == "__main__":
    import sys

    asyncio.run(main(apply="--apply" in sys.argv))
