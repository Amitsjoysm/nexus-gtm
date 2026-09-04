# tests/test_billing_adversarial.py
"""What a customer, an admin, or a replayed webhook can do to the money if they try.

Every case here is something that either costs us revenue or costs a customer money they did not
spend. They are grouped by who is holding the lever, because the defences are different: a
customer can only reach the API, an admin can reach the rate card, and a payment provider can
send the same event four times.

The rule this file exists to hold: **no input reachable from outside can make a charge negative,
free, or doubled.**
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


_SLUG = iter(f"adv{i}" for i in range(1, 999))


async def _funded(plan_id: str = "launch", credits: float = 10_000):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    slug = next(_SLUG)          # distinct per call: two tenants in one test must not collide
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
        await grant_credits(ts, credits, reason="seed", idempotency_key="seed")
    return tid


async def _balance(ts) -> float:
    from nexus.billing.credits import balance

    return await balance(ts)


# --------------------------------------------------------------- what a CUSTOMER can send
class TestCustomerSuppliedInput:
    """The metering seam takes a quantity. Everything below is a value someone could put there."""

    async def test_a_negative_quantity_never_credits_the_caller(self, enforcing):
        """Usage is SUMMED, so a negative would rewind the counter and hand back quota — and if it
        reached the burn it would pay the customer to use the product."""
        from nexus.billing.entitlements import check_and_meter

        tid = await _funded()
        async with tenant_session(tid) as ts:
            before = await _balance(ts)
            res = await check_and_meter(
                ts, capability_id="ai.email_draft", quantity=-500, idempotency_key="neg"
            )
            after = await _balance(ts)
        assert after <= before, f"a negative quantity ADDED {after - before} credits"
        assert res.recorded is False, "a negative quantity was written into the usage stream"

    async def test_zero_is_not_a_free_action(self, enforcing):
        from nexus.billing.entitlements import check_and_meter

        tid = await _funded()
        async with tenant_session(tid) as ts:
            res = await check_and_meter(
                ts, capability_id="ai.email_draft", quantity=0, idempotency_key="zero"
            )
            assert res.recorded is False

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    async def test_nan_and_infinity_are_refused(self, enforcing, bad):
        """`inf` in a SUM poisons every later quota read for that tenant."""
        from nexus.billing.entitlements import check_and_meter

        tid = await _funded()
        async with tenant_session(tid) as ts:
            before = await _balance(ts)
            res = await check_and_meter(
                ts, capability_id="ai.email_draft", quantity=bad, idempotency_key=f"b{bad}"
            )
            assert res.recorded is False
            assert await _balance(ts) == before

    async def test_an_absurd_quantity_cannot_drain_a_balance_in_one_call(self, enforcing):
        """A unit-conversion bug or a hostile caller must not empty an account in one request."""
        from nexus.billing.entitlements import MAX_QUANTITY_PER_CALL, check_and_meter

        tid = await _funded()
        async with tenant_session(tid) as ts:
            before = await _balance(ts)
            res = await check_and_meter(
                ts, capability_id="ai.email_draft",
                quantity=MAX_QUANTITY_PER_CALL * 1000, idempotency_key="huge",
            )
            assert res.recorded is False
            assert await _balance(ts) == before

    async def test_spending_stops_at_zero_rather_than_going_negative(self, enforcing):
        """The wall. A customer with no balance must not be able to run up a debt silently."""
        from nexus.billing.entitlements import check_and_meter

        tid = await _funded(credits=4)          # enough for exactly two email drafts at 2
        async with tenant_session(tid) as ts:
            assert (await check_and_meter(ts, capability_id="ai.email_draft",
                                          idempotency_key="a")).allowed is True
            assert (await check_and_meter(ts, capability_id="ai.email_draft",
                                          idempotency_key="b")).allowed is True
            third = await check_and_meter(ts, capability_id="ai.email_draft",
                                          idempotency_key="c")
            assert third.allowed is False
            assert await _balance(ts) >= 0, "the balance went negative"


# --------------------------------------------------------------- replay and duplication
class TestReplay:
    async def test_the_same_request_replayed_ten_times_is_charged_once(self, enforcing):
        from nexus.billing.entitlements import check_and_meter

        tid = await _funded()
        async with tenant_session(tid) as ts:
            before = await _balance(ts)
            for _ in range(10):
                await check_and_meter(
                    ts, capability_id="ai.email_draft", quantity=5, idempotency_key="same"
                )
            spent = before - await _balance(ts)
        assert spent == 10, f"ten replays of one 5-unit call cost {spent}, not 10"

    async def test_one_tenants_key_cannot_settle_anothers_charge(self, enforcing):
        """Idempotency is scoped `(tenant_id, key)`. If it were global, a customer who learned a
        common key would get everything after the first request free."""
        from nexus.billing.entitlements import check_and_meter

        a, b = await _funded(), await _funded()
        async with tenant_session(a) as ts:
            before_a = await _balance(ts)
            await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="shared")
            spent_a = before_a - await _balance(ts)
        async with tenant_session(b) as ts:
            before_b = await _balance(ts)
            await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="shared")
            spent_b = before_b - await _balance(ts)
        assert spent_a == spent_b == 2, "a shared key let one tenant ride on another's payment"

    async def test_a_replayed_credit_grant_grants_once(self):
        from nexus.billing.credits import grant_credits

        tid = await _funded(credits=0)
        async with tenant_session(tid) as ts:
            for _ in range(5):
                await grant_credits(ts, 500, reason="promo", idempotency_key="promo-2026")
            assert await _balance(ts) == 500

    async def test_a_replayed_plan_upgrade_grants_once(self):
        """A webhook Stripe retries four times must not deliver four months of credits."""
        from nexus.billing.subscriptions import apply_plan_change_credits
        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.billing import BillingPlan

        tid = await _funded(credits=0)
        async with get_platform_sessionmaker()() as s:
            launch = await s.get(BillingPlan, "launch")
        async with tenant_session(tid) as ts:
            for _ in range(4):
                await apply_plan_change_credits(ts, launch)
            assert await _balance(ts) == launch.included_credits


# --------------------------------------------------------------- what an ADMIN can set
class TestAdminSuppliedPricing:
    def test_a_negative_price_is_refused_even_with_a_margin_exception(self):
        """`gross_margin` returns 0.0 for any non-positive revenue, so a NEGATIVE price fails the
        floor for the same reason a free one does — and is therefore waved through the moment
        somebody records a margin exception, which is a normal thing for finance to do.

        A negative rate does not credit the customer (the burn floors at zero), but it makes the
        capability free while the console displays a price. Refusing it outright is the only
        reading of "credits_per_unit = -5" that is not a mistake.
        """
        from nexus.billing.rates import MarginFloorError, validate_rate

        with pytest.raises((MarginFloorError, ValueError)):
            validate_rate("ai.email_draft", credits_per_unit=-5, unit_cost_usd=0.01,
                          margin_exception=True)

    def test_a_zero_price_is_still_allowed_with_an_exception(self):
        """Free capabilities are legitimate — `module.*` gates and anything bundled."""
        from nexus.billing.rates import validate_rate

        assert validate_rate("module.agents", credits_per_unit=0, unit_cost_usd=0,
                             margin_exception=True) == 0.0

    async def test_a_credit_grant_cannot_be_negative_or_zero(self):
        """The endpoint pins `gt=0`; this holds the service under it, which the worker also calls."""
        from nexus.billing.credits import grant_credits

        tid = await _funded(credits=100)
        async with tenant_session(tid) as ts:
            assert await grant_credits(ts, -1000, reason="x", idempotency_key="n1") is False
            assert await grant_credits(ts, 0, reason="x", idempotency_key="n2") is False
            assert await _balance(ts) == 100


# --------------------------------------------------------------- tenant isolation
class TestIsolation:
    async def test_a_tenant_cannot_see_or_spend_another_tenants_credits(self):
        from nexus.billing.credits import burn_credits

        rich = await _funded(credits=50_000)
        poor = await _funded(credits=0)
        async with tenant_session(poor) as ts:
            assert await _balance(ts) == 0
            assert await burn_credits(ts, 10, idempotency_key="steal") is False
        async with tenant_session(rich) as ts:
            assert await _balance(ts) == 50_000, "another tenant's burn reached this balance"

    async def test_usage_recorded_for_one_tenant_does_not_count_against_another(self, enforcing):
        from nexus.billing.entitlements import current_usage
        from nexus.billing.usage import record_usage

        a, b = await _funded(), await _funded()
        async with tenant_session(a) as ts:
            await record_usage(ts, capability_id="ai.email_draft", quantity=900,
                               idempotency_key="bulk")
        async with tenant_session(b) as ts:
            assert await current_usage(ts, "ai.email_draft") == 0
