# tests/test_plan_ladder.py
"""The public ladder: Free, Launch, Accelerate, and the two annuals.

Collapsed from eight tiers on 2026-08-26. The superseded ones are RETIRED, never deleted: three
subscriptions point at `core` and `growth`, `billing_subscriptions.plan_id` is a foreign key, and
entitlements resolve from the plan ROW. A deleted plan is either a constraint violation or a paying
customer with no entitlements at all — which falls back to permissive catalog defaults and hands
them everything.
"""
from __future__ import annotations


async def test_the_ladder_is_free_launch_accelerate(fresh_db):
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        rows = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}

    for pid, price, credits, interval in (
        ("launch", 9900, 2500, "month"),
        ("accelerate", 19900, 8000, "month"),
        ("launch-annual", 95000, 30000, "year"),
        ("accelerate-annual", 191000, 96000, "year"),
    ):
        plan = rows.get(pid)
        assert plan is not None, f"{pid} missing"
        assert plan.base_price_cents == price, pid
        assert plan.included_credits == credits, pid
        assert plan.interval == interval, pid
        assert plan.plan_class == "standard", pid

    assert rows["free"].included_credits == 1000, "free must be enough to try every feature"


async def test_the_annuals_are_twenty_percent_off(fresh_db):
    """Stated as a rule, not a number. The discount is the product decision and the price is
    arithmetic — so if someone reprices a monthly tier, this catches the annual left behind."""
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        rows = {p.id: p for p in (await s.scalars(select(BillingPlan))).all()}

    for monthly, annual in (("launch", "launch-annual"), ("accelerate", "accelerate-annual")):
        full_year = rows[monthly].base_price_cents * 12
        discount = 1 - (rows[annual].base_price_cents / full_year)
        assert 0.19 <= discount <= 0.21, f"{annual} is {discount:.1%} off, expected ~20%"
        assert rows[annual].included_credits == rows[monthly].included_credits * 12


async def test_the_ladder_stays_inside_the_break_even_ceiling(fresh_db):
    """The dearest capability costs $0.00400 per credit, so a dollar buys 250 credits at cost.
    100 cr/$ is the design rule — 60% margin with buffer. A plan above it is losing money on a
    customer who spends their allowance on the expensive end of the catalog."""
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(
            select(BillingPlan).where(BillingPlan.plan_class == "standard")
        )).all()

    for plan in rows:
        if not plan.base_price_cents or not plan.included_credits:
            continue
        per_dollar = plan.included_credits / (plan.base_price_cents / 100)
        assert per_dollar <= 100, (
            f"{plan.id} sells {per_dollar:.1f} credits per dollar, past the 100 design ceiling"
        )


async def test_seeding_never_removes_a_plan(fresh_db):
    """The safety property of the whole restructure. `legacy-unlimited` is the migration keystone
    for 13 workspaces."""
    from sqlalchemy import select

    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_plans()
    await sync_plans()
    async with get_sessionmaker()() as s:
        ids = {p.id for p in (await s.scalars(select(BillingPlan))).all()}
    assert "legacy-unlimited" in ids
    assert "free" in ids


async def test_free_is_on_the_price_list(client, fresh_db):
    """A price list that hides the free option is not a price list, it is a paywall with a gap."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="pl1", email="o@pl1.com", company="PL1")

    rows = (await client.get("/api/billing/plans", headers=auth(token))).json()
    ids = [p["id"] for p in rows]
    assert "free" in ids
    assert "launch" in ids and "accelerate" in ids
    # The retired tiers are gone from the customer's view without their subscribers moving.
    assert "growth" not in ids and "core" not in ids


async def test_free_is_listed_but_cannot_be_checked_out(client, fresh_db):
    """Listed to be seen, not bought. A $0 hosted checkout would create a Stripe product for
    something that never charges anyone, and put a card form in front of a downgrade."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="pl2", email="o@pl2.com", company="PL2")

    r = await client.post("/api/billing/checkout", headers=auth(token),
                          json={"plan_id": "free"})
    assert r.status_code == 409
    assert "nothing to check out" in r.text
