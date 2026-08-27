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
# free tier, which does the same job of letting someone try the product without committing, and
# does it without an invoice.
RETIRE = (
    "core", "starter", "growth", "professional", "business",
    "scale", "scale-annual", "payg", "payg-annual",
)

KEEP_ACTIVE = ("free", "launch", "launch-annual", "accelerate", "accelerate-annual")

# The intended allowance for each live tier, mirroring `PLAN_SEED`.
#
# This exists because `sync_plans` deliberately **never overwrites a live row** — once shipped,
# pricing belongs to Admin rather than to a redeploy. So editing the seed moves a fresh install and
# nothing else; an established deployment is moved here, on purpose, by someone who read the dry run.
#
# Keep it in step with `PLAN_SEED`, which `test_the_restructure_script_matches_the_seed` asserts.
# Drifting the two apart means a fresh install and an upgraded one sell different products under
# one name — a difference nothing in the running system would report.
ALLOWANCES: dict[str, tuple[int, str]] = {
    "free": (200, "Try every feature with 200 credits."),
    "launch": (2000, "2,000 credits a month for a team getting started."),
    "launch-annual": (24000, "24,000 credits a year - two months free."),
    "accelerate": (8000, "8,000 credits a month for a team running full pipeline."),
    "accelerate-annual": (96000, "96,000 credits a year - two months free."),
}


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

        # Resize allowances to match the seed.
        #
        # A CUT is called out separately and names the subscriber count, because it is the one
        # action here that takes something away from someone who is already paying. Retiring a plan
        # leaves its subscribers exactly where they are; reducing an allowance does not. Whether
        # that is acceptable depends on what those customers were sold, which is a commercial
        # question this script must surface rather than answer.
        changes = []
        for plan_id, (credits, description) in ALLOWANCES.items():
            plan = plans.get(plan_id)
            if plan is None or plan.included_credits == credits:
                continue
            changes.append((plan_id, plan.included_credits, credits, counts.get(plan_id, 0)))
            if apply:
                plan.included_credits = credits
                plan.description = description

        if changes:
            print("\n  allowances")
            print("  " + "-" * 70)
            for plan_id, before, after, subs in changes:
                direction = "CUT " if after < before else "RAISE"
                warn = f"  <- {subs} live subscriber(s) LOSE credits" if (
                    after < before and subs) else ""
                print(f"  {plan_id:22}{direction} {before:>7,} -> {after:>7,}{warn}")

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
