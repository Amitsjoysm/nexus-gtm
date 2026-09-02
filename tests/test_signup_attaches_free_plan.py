# tests/test_signup_attaches_free_plan.py
"""A new workspace starts on `free`, and the backfill must never take it off again.

Observed in production 2026-09-01: five tenants, ALL on `legacy-unlimited`, all active, all
``grandfathered=True`` with no ``psp_subscription_id`` — including workspaces created days after
billing was deployed. Two causes compounding:

1. **Tenant creation never created a subscription.** `ensure_subscription` was called only from the
   admin endpoints and from the backfill itself; `/auth/signup`, `/auth/workspaces` and the OTP
   verify path all created a Tenant and attached no plan. The entitlement engine's documented
   "tenant with no subscription -> allow" default then granted the workspace everything.
2. **The startup backfill swept them up.** It runs on EVERY app start and put every tenant with no
   subscription onto `legacy-unlimited` with ``grandfathered=True``. It was written as a one-time
   migration for tenants predating billing, and had no way to tell those from a tenant created five
   minutes ago.

So: sign up -> no subscription -> next restart -> grandfathered onto unlimited, permanently.

Latent while ``NEXUS_BILLING_ENFORCEMENT`` is ``shadow``, because free and legacy-unlimited behave
identically there. It becomes revenue leakage the moment enforcement is armed: a grandfathered
tenant can never hit a paywall or an upgrade prompt.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.models.billing import BillingSubscription
from nexus.models.identity import Tenant
from tests.conftest import auth, signup


async def _subscription_for(slug: str):
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == slug))).first()
        assert tid, f"tenant {slug} was not created"
        return (
            await s.scalars(
                select(BillingSubscription).where(BillingSubscription.tenant_id == tid)
            )
        ).first()


async def test_signup_puts_the_workspace_on_free(client, fresh_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    await signup(client, slug="fresh1", email="a@fresh1.com", company="Fresh1")

    sub = await _subscription_for("fresh1")
    assert sub is not None, "signup created a tenant with NO subscription at all"
    assert sub.plan_id == "free", f"new signup landed on {sub.plan_id!r}"
    assert sub.grandfathered is False, (
        "a workspace created today is not a legacy tenant; grandfathering it means it can never "
        "hit a paywall"
    )
    assert sub.status == "active"


async def test_the_free_plan_credits_are_granted(client, fresh_db):
    """The plan row alone is not enough — a subscription with no credit grant has nothing to
    spend, so the first metered call would fail for the wrong reason."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import balance
    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await sync_catalog()
    await sync_plans()
    await signup(client, slug="fresh2", email="a@fresh2.com", company="Fresh2")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "fresh2"))).first()
        assert await balance(TenantSession(s, tid)) == 200, (
            "the free plan's 200 included credits were not granted at signup"
        )


async def test_the_backfill_does_not_move_a_new_signup(client, fresh_db):
    """THE regression this file exists for. The backfill runs on every app start."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.subscriptions import backfill_subscriptions

    await sync_catalog()
    await sync_plans()
    await signup(client, slug="fresh3", email="a@fresh3.com", company="Fresh3")

    before = await _subscription_for("fresh3")
    await backfill_subscriptions()
    after = await _subscription_for("fresh3")

    assert after.plan_id == before.plan_id == "free"
    assert after.grandfathered is False
    assert after.id == before.id, "the backfill created a SECOND subscription row"


async def test_the_backfill_leaves_a_tenant_created_after_billing_alone(fresh_db):
    """Even with no subscription at all — the case where signup's attach failed — a tenant created
    AFTER billing shipped must not be grandfathered. Its permissive state is then the engine's
    "no subscription -> allow" default, which is recoverable; a grandfathered row is not."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.core.db import get_sessionmaker

    await sync_catalog()
    await sync_plans()

    async with get_sessionmaker()() as s:
        tenant = Tenant(name="Newer", slug="newer")
        s.add(tenant)
        await s.commit()
        tid = tenant.id

    await backfill_subscriptions()

    async with get_sessionmaker()() as s:
        sub = (
            await s.scalars(
                select(BillingSubscription).where(BillingSubscription.tenant_id == tid)
            )
        ).first()
    assert sub is None, (
        "a tenant created after billing shipped was grandfathered onto legacy-unlimited by the "
        "startup backfill — the exact bug"
    )


async def test_the_backfill_still_covers_a_genuinely_old_tenant(fresh_db):
    """The migration must keep working. A tenant predating the earliest billing_plans row is what
    legacy-unlimited exists for, and arming enforcement without it would break those customers."""
    from datetime import timedelta

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import LEGACY_PLAN_ID, sync_plans
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.billing import BillingPlan

    await sync_catalog()
    await sync_plans()

    async with get_sessionmaker()() as s:
        earliest = (await s.scalars(select(BillingPlan.created_at))).first()
        tenant = Tenant(name="Ancient", slug="ancient")
        tenant.created_at = (earliest or utcnow()) - timedelta(days=365)
        s.add(tenant)
        await s.commit()
        tid = tenant.id

    await backfill_subscriptions()

    async with get_sessionmaker()() as s:
        sub = (
            await s.scalars(
                select(BillingSubscription).where(BillingSubscription.tenant_id == tid)
            )
        ).first()
    assert sub is not None, "a genuinely pre-billing tenant was left with no subscription"
    assert sub.plan_id == LEGACY_PLAN_ID
    assert sub.grandfathered is True


async def test_the_backfill_never_downgrades_an_upgraded_tenant(client, fresh_db):
    """The property the original backfill had and must keep: a redeploy can never move a paying
    customer."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.subscriptions import backfill_subscriptions, change_plan
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await sync_catalog()
    await sync_plans()
    await signup(client, slug="paid1", email="a@paid1.com", company="Paid1")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "paid1"))).first()
        ts = TenantSession(s, tid)
        await change_plan(ts, "accelerate", actor="test")
        await s.commit()

    await backfill_subscriptions()
    sub = await _subscription_for("paid1")
    assert sub.plan_id == "accelerate", "the backfill downgraded a paying customer"
