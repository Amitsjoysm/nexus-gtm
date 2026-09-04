#!/usr/bin/env python
"""Move an established deployment onto the current seat ladder.

`sync_plans` never mutates an existing plan — that is what stops a redeploy repricing a live
customer — so changing the seed reaches fresh installs only. This moves the rows.

    python scripts/restructure_seats.py            # dry run, prints what would change
    python scripts/restructure_seats.py --apply    # writes

**Shrinking a seat count can lock people out.** `seat.member` is a gauge that resolves to live
membership, and the invite path now refuses when it is full — so a plan cut below what a customer
already uses does not just stop the next hire, it leaves them permanently over their limit. Every
run therefore reports affected workspaces first, and `--apply` REFUSES to shrink a plan past its
largest current tenant unless `--force` is given. There is no reading of "lock out a paying
customer" that should be one flag away.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--force", action="store_true",
                    help="shrink a plan even where a tenant is already over it")
    args = ap.parse_args()

    from sqlalchemy import func, select

    from nexus.billing.plans import PLAN_SEED, seat_quota_for
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan, BillingPlanEntitlement, BillingSubscription
    from nexus.models.identity import Membership

    async with get_platform_sessionmaker()() as s:
        plans = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}
        ents = {
            e.plan_id: e
            for e in (
                await s.scalars(
                    select(BillingPlanEntitlement).where(
                        BillingPlanEntitlement.capability_id == "seat.member"
                    )
                )
            ).all()
        }
        # Largest live membership on each plan — the floor below which a cut locks someone out.
        rows = (
            await s.execute(
                select(BillingSubscription.plan_id, func.count(Membership.id))
                .join(Membership, Membership.tenant_id == BillingSubscription.tenant_id)
                .group_by(BillingSubscription.plan_id, BillingSubscription.tenant_id)
            )
        ).all()
        largest: dict[str, int] = {}
        for plan_id, n in rows:
            largest[plan_id] = max(largest.get(plan_id, 0), int(n or 0))

        print(f"{'plan':22} {'now':>6} {'target':>7} {'largest tenant':>15}  action")
        changes, blocked = [], []
        for spec in PLAN_SEED:
            pid = spec["id"]
            target = seat_quota_for(pid)
            plan = plans.get(pid)
            if plan is None or target is None:
                continue
            ent = ents.get(pid)
            current = ent.quota if ent is not None else plan.max_seats
            in_use = largest.get(pid, 0)
            if current == target and plan.max_seats == target:
                continue
            if target < in_use and not args.force:
                blocked.append((pid, current, target, in_use))
                action = "** REFUSED: a tenant already has more members than this **"
            else:
                changes.append((pid, current, target, ent, plan))
                action = "would set" if not args.apply else "setting"
            print(f"{pid:22} {str(current):>6} {target:>7} {in_use:>15}  {action}")

        if not changes and not blocked:
            print("\nnothing to do — the ladder already matches the seed")
            return 0

        if blocked:
            print(f"\n{len(blocked)} plan(s) refused. Raise the target, move those tenants first, "
                  "or re-run with --force if you accept locking them out.")

        if not args.apply:
            print(f"\ndry run — {len(changes)} plan(s) would change. Re-run with --apply.")
            return 0

        for pid, _current, target, ent, plan in changes:
            plan.max_seats = target
            if ent is not None:
                ent.quota = target
            else:
                s.add(BillingPlanEntitlement(
                    plan_id=pid, capability_id="seat.member", mode="metered", quota=target,
                ))
        await s.commit()
        print(f"\napplied to {len(changes)} plan(s)")
        return 1 if blocked else 0


sys.exit(asyncio.run(main()))
