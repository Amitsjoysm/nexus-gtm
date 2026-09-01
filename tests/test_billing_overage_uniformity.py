# tests/test_billing_overage_uniformity.py
"""One unit past the quota costs the same whichever request carries it.

BILL-01 fixed the headline case — `over` was the running total by which a tenant was past the
line rather than the part THIS call crossed, so the Nth overage unit cost N times its price. These
assert the property that fix was aiming at, across the shapes the original regression test did not
cover: many single-unit calls, one large call, a call straddling the line, and a rate card
carrying a volume ladder.

The last one is a second, independent way the price can move: the in-flight burn read
`card.credits_per_unit` flat while `rating.rate_period` priced the same units through
`tiered_credits`. No seeded card has tiers today, but Admin can add one through
`PUT /admin/billing/rates/{capability_id}` — and on the day someone does, what the customer's
balance was debited and what their invoice says stop agreeing.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "free", slug: str = "t1"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant(slug=slug, name=slug)
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


async def test_ten_successive_single_unit_calls_debit_the_same_amount(enforcing):
    """The original bug grew the price by one unit per call; ten calls made it obvious."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage

    tid = await _seed()          # ai.email_draft quota 20, card 2 credits/unit
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10_000, reason="x", idempotency_key="g")
        await record_usage(ts, capability_id="ai.email_draft", quantity=20,
                           idempotency_key="pre")

        debits, before = [], await balance(ts)
        for n in range(10):
            res = await check_and_meter(
                ts, capability_id="ai.email_draft", idempotency_key=f"o{n}"
            )
            assert res.allowed is True
            after = await balance(ts)
            debits.append(before - after)
            before = after

    assert debits == [2] * 10, f"per-unit price drifted across requests: {debits}"


async def test_one_large_call_costs_the_same_as_the_same_units_split_up(enforcing):
    """Batching must not change the bill. Ten units in one call = ten units in ten calls."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage

    async def spend(split: bool) -> float:
        tid = await _seed(slug="split" if split else "bulk")
        async with tenant_session(tid) as ts:
            await grant_credits(ts, 10_000, reason="x", idempotency_key="g")
            await record_usage(ts, capability_id="ai.email_draft", quantity=20,
                               idempotency_key="pre")
            start = await balance(ts)
            if split:
                for n in range(10):
                    await check_and_meter(ts, capability_id="ai.email_draft",
                                          idempotency_key=f"s{n}")
            else:
                await check_and_meter(ts, capability_id="ai.email_draft", quantity=10,
                                      idempotency_key="bulk")
            return start - await balance(ts)

    assert await spend(split=True) == await spend(split=False) == 20


async def test_a_call_straddling_the_quota_pays_only_for_the_part_over(enforcing):
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10_000, reason="x", idempotency_key="g")
        await record_usage(ts, capability_id="ai.email_draft", quantity=18,
                           idempotency_key="pre")   # 2 left of 20
        start = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", quantity=5,
                              idempotency_key="straddle")
        # 3 units over x 2 credits. The 2 inside the quota are already paid for.
        assert start - await balance(ts) == 6


async def test_a_tiered_card_debits_what_the_invoice_will_charge(enforcing):
    """In-flight burn and period-close rating must price the same units identically.

    Otherwise the balance is debited at the flat rate and the invoice is computed on the ladder,
    and the difference is a discrepancy no one can explain to the customer.
    """
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import tiered_credits
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingRateCard

    tid = await _seed()
    async with tenant_session(tid) as ts:
        # First 5 overage units at 2 credits, everything beyond at 1.
        card = await ts.session.get(BillingRateCard, "ai.email_draft")
        card.credits_per_unit = 2
        card.tiers = [{"upto": 5, "credits": 2}, {"upto": None, "credits": 1}]
        await ts.flush()

        await grant_credits(ts, 10_000, reason="x", idempotency_key="g")
        await record_usage(ts, capability_id="ai.email_draft", quantity=20,
                           idempotency_key="pre")

        start = await balance(ts)
        await check_and_meter(ts, capability_id="ai.email_draft", quantity=8,
                              idempotency_key="tiered")
        debited = start - await balance(ts)

        expected = tiered_credits(8, card)      # 5*2 + 3*1 = 13
        assert expected == 13
        assert debited == expected, (
            f"burned {debited} credits in flight but the invoice prices these units at "
            f"{expected} — the two paths disagree"
        )
