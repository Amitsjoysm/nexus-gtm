# tests/test_plan_change_grants_credits.py
"""Upgrading a plan must deliver the credits that plan is sold with.

Reported from production: a customer subscribed to Launch through Stripe Checkout, saw the plan
change to Launch, and had **zero credits**. Both halves of that are real and independent:

* `_apply_subscription_event` sets `sub.plan_id` from the Checkout metadata and stops. The webhook
  never grants credits, so the plan the customer is now paying for arrives empty.
* Even if it did, `grant_plan_credits` keys its grant `plan_grant:{period}` — by PERIOD ONLY. A
  workspace signs up on `free`, takes that key with a 200-credit grant, and any upgrade in the
  same month finds the key already used and grants nothing. The customer pays for 2,000 credits
  and receives whatever is left of the free tier's 200.

The fix is a plan-scoped grant on change, keyed `plan_change:{plan}:{period}` rather than reusing
the period key. That is deliberate: rewriting the EXISTING key to include the plan would make
`plan_grant:{period}` and `plan_grant:free:{period}` two different keys, so every tenant already
granted this period would be granted a second time on the next roll.
"""
from __future__ import annotations


from tests.conftest import make_tenant, tenant_session


async def _on_free():
    """A workspace that signed up this period and took the free grant, as production does."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.subscriptions import start_subscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await start_subscription(ts, plan_id="free")
    return tid


async def _balance(ts) -> float:
    from nexus.billing.credits import balance

    return await balance(ts)


async def _plan(plan_id: str):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingPlan

    async with get_platform_sessionmaker()() as s:
        return await s.get(BillingPlan, plan_id)


async def test_signing_up_on_free_grants_the_free_credits():
    """The baseline this bug hides behind — signup works, which is why nobody looked further."""
    tid = await _on_free()
    async with tenant_session(tid) as ts:
        assert await _balance(ts) == 200


async def test_upgrading_mid_period_grants_the_new_plan_credits():
    """The reported failure: Launch shows as the plan, and the credits never arrive."""
    from nexus.billing.subscriptions import apply_plan_change_credits

    tid = await _on_free()
    launch = await _plan("launch")
    assert launch.included_credits == 2000, "the ladder moved; update this test"

    async with tenant_session(tid) as ts:
        before = await _balance(ts)
        granted = await apply_plan_change_credits(ts, launch)
        after = await _balance(ts)

    assert granted is True
    assert after - before == 2000, (
        f"upgrading to Launch granted {after - before} credits, not its 2,000 — the customer "
        "is paying for a plan they did not receive"
    )


async def test_the_free_grant_does_not_block_the_upgrade():
    """The precise mechanism: a period-keyed grant already exists when the upgrade lands."""
    from nexus.billing.rollups import period_key
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingCreditLedger
    from sqlalchemy import select

    tid = await _on_free()
    pk = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        keys = [
            r.idempotency_key
            for r in (
                await ts.session.scalars(
                    select(BillingCreditLedger).where(BillingCreditLedger.tenant_id == tid)
                )
            ).all()
        ]
    assert f"plan_grant:{pk}" in keys, "signup no longer uses the period key; revisit this"


async def test_upgrading_twice_in_one_period_grants_once():
    """The key is per plan per period, so a plan cannot be farmed by switching back and forth."""
    from nexus.billing.subscriptions import apply_plan_change_credits

    tid = await _on_free()
    launch = await _plan("launch")
    async with tenant_session(tid) as ts:
        await apply_plan_change_credits(ts, launch)
        after_first = await _balance(ts)
        await apply_plan_change_credits(ts, launch)
        await apply_plan_change_credits(ts, launch)
        assert await _balance(ts) == after_first


async def test_moving_between_two_plans_grants_each_once():
    """A genuine upgrade path — free -> launch -> accelerate — delivers both, once each."""
    from nexus.billing.subscriptions import apply_plan_change_credits

    tid = await _on_free()
    launch = await _plan("launch")
    accelerate = await _plan("accelerate")
    async with tenant_session(tid) as ts:
        await apply_plan_change_credits(ts, launch)
        await apply_plan_change_credits(ts, accelerate)
        assert await _balance(ts) == 200 + launch.included_credits + accelerate.included_credits


async def test_a_plan_with_no_included_credits_grants_nothing():
    """Enterprise and custom deals are invoiced on other terms; a zero grant is normal."""
    from nexus.billing.subscriptions import apply_plan_change_credits

    tid = await _on_free()
    async with tenant_session(tid) as ts:
        before = await _balance(ts)
        assert await apply_plan_change_credits(ts, None) is False
        assert await _balance(ts) == before


async def test_the_stripe_webhook_grants_on_a_plan_change():
    """The path the customer actually took. Structural, because reproducing a signed Checkout
    event here would test the fixture rather than the wiring."""
    import inspect

    from nexus.billing import webhooks

    applied = inspect.getsource(webhooks._apply_subscription_event)
    assert "_grant_plan_credits" in applied, (
        "the Stripe webhook sets plan_id and never grants the plan's credits — which is exactly "
        "what the customer reported"
    )
    # And the helper reaches the real grant rather than being a stub.
    assert "apply_plan_change_credits" in inspect.getsource(webhooks._grant_plan_credits)
    # Both event families can carry the new plan; both must grant.
    assert applied.count("_grant_plan_credits(") == 2, (
        "checkout.session.completed and customer.subscription.* can each deliver the plan; "
        "whichever arrives first must grant, and the per-plan key makes the second a no-op"
    )
