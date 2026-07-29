# tests/test_billing_subscriptions.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def test_backfill_gives_every_tenant_the_legacy_plan():
    """The safety keystone: an un-subscribed tenant must never be mis-gated when enforcement
    is armed, so everyone who predates billing lands on unlimited + grandfathered."""
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    t1 = await make_tenant(slug="bf1", name="BF One")
    t2 = await make_tenant(slug="bf2", name="BF Two")

    assert (await backfill_subscriptions())["created"] == 2
    for tid in (t1, t2):
        async with tenant_session(tid) as ts:
            subs = await ts.list(BillingSubscription)
            assert len(subs) == 1
            assert subs[0].plan_id == LEGACY_PLAN_ID
            assert subs[0].status == "active"
            assert subs[0].grandfathered is True


async def test_backfill_is_idempotent():
    from nexus.billing.subscriptions import backfill_subscriptions

    await _seed()
    await make_tenant(slug="bf3", name="BF Three")
    assert (await backfill_subscriptions())["created"] == 1
    assert (await backfill_subscriptions())["created"] == 0     # a redeploy changes nothing


async def test_backfill_never_overwrites_a_paying_tenant():
    """A tenant who has since upgraded must not be silently downgraded by a redeploy."""
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="bf4", name="BF Four")
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active"))
        await ts.flush()

    assert (await backfill_subscriptions())["created"] == 0
    async with tenant_session(tid) as ts:
        subs = await ts.list(BillingSubscription)
        assert len(subs) == 1 and subs[0].plan_id == "growth"


async def test_backfill_sets_a_billing_period():
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="bf5", name="BF Five")
    await backfill_subscriptions()
    async with tenant_session(tid) as ts:
        sub = (await ts.list(BillingSubscription))[0]
        assert sub.current_period_start is not None
        assert sub.current_period_end is not None
        assert sub.current_period_end > sub.current_period_start


async def test_change_plan_switches_the_active_subscription():
    from nexus.billing.subscriptions import change_plan, ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="cp1", name="CP One")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="free")
        sub = await change_plan(ts, "growth", actor="admin@nexus")
        assert sub.plan_id == "growth" and sub.status == "active"
        assert len(await ts.list(BillingSubscription)) == 1     # switched, not duplicated
        assert sub.meta["previous_plan_id"] == "free"           # auditable


async def test_change_plan_clears_grandfathering():
    """Choosing a new plan is choosing its current terms; frozen legacy pricing does not
    survive an upgrade."""
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.billing.subscriptions import change_plan, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp2", name="CP Two")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id=LEGACY_PLAN_ID, grandfathered=True)
        sub = await change_plan(ts, "growth", actor="admin@nexus")
        assert sub.grandfathered is False


async def test_change_plan_rejects_an_unknown_plan():
    from nexus.billing.subscriptions import change_plan, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp3", name="CP Three")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="free")
        try:
            await change_plan(ts, "no-such-plan", actor="admin@nexus")
        except ValueError:
            return
        raise AssertionError("change_plan must reject an unknown plan")


async def test_cancel_at_period_end_keeps_service_running():
    """Cancelling is not cutting off — the customer paid through the period."""
    from nexus.billing.subscriptions import cancel_subscription, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp4", name="CP Four")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="growth")
        sub = await cancel_subscription(ts, at_period_end=True)
        assert sub.cancel_at_period_end is True
        assert sub.status == "active"


async def test_period_roll_closes_rates_and_advances():
    """Close the books: rate the period, finalize the invoice, grant the new period's credits,
    then advance the window."""
    from nexus.billing.subscriptions import ensure_subscription, roll_period
    from nexus.billing.credits import balance
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice, BillingSubscription
    from datetime import timedelta

    await _seed()
    tid = await make_tenant(slug="rp1", name="RP One")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")   # 2000 included credits
        sub.current_period_end = utcnow() - timedelta(minutes=1)   # due
        await ts.flush()

        rolled = await roll_period(ts)
        assert rolled is True

        inv = (await ts.list(BillingInvoice))[0]
        assert inv.status == "finalized"

        assert await balance(ts) == 2000                        # new period's credits granted

        sub = (await ts.list(BillingSubscription))[0]
        assert sub.current_period_end > utcnow()                # window advanced


async def test_period_roll_is_a_noop_before_the_period_ends():
    from nexus.billing.subscriptions import ensure_subscription, roll_period

    await _seed()
    tid = await make_tenant(slug="rp2", name="RP Two")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="growth")          # period ends in a month
        assert await roll_period(ts) is False


async def test_period_roll_grants_credits_exactly_once():
    """Idempotency at the money boundary: a retried job must not double-grant."""
    from nexus.billing.credits import balance
    from nexus.billing.subscriptions import ensure_subscription, roll_period
    from nexus.core.db import utcnow
    from datetime import timedelta

    await _seed()
    tid = await make_tenant(slug="rp3", name="RP Three")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        await roll_period(ts)
        first = await balance(ts)

        # Force it due again and re-roll within the same calendar month. The grant is keyed by
        # period, so the second roll resolves to the same key and must not grant again.
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        await roll_period(ts)
        assert await balance(ts) == first           # exactly once, not twice
