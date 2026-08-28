# tests/test_billing_overage_increment.py
"""Overage must be charged per unit that goes over, not per unit of cumulative excess.

Every pre-existing overage test fires exactly ONE call past the quota, where "units this call
pushed over" and "total units over the limit" happen to be the same number. They diverge on the
second call and every one after it.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "free"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


async def test_each_overage_unit_costs_the_same(enforcing):
    """Three single-unit actions past a quota of 20 cost 3 x 2 credits, not 2 + 4 + 6."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage

    tid = await _seed()          # ai.email_draft quota 20, rate card 2 credits/unit
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await record_usage(ts, capability_id="ai.email_draft", quantity=20,
                           idempotency_key="pre")

        spend = []
        before = await balance(ts)
        for n in range(3):
            res = await check_and_meter(
                ts, capability_id="ai.email_draft", idempotency_key=f"over{n}"
            )
            assert res.allowed is True
            after = await balance(ts)
            spend.append(before - after)
            before = after

        assert spend == [2, 2, 2], f"per-unit overage price drifted across calls: {spend}"
        assert await balance(ts) == 1000 - 6


async def test_a_multi_unit_call_only_pays_for_the_part_over_the_line(enforcing):
    """A call of 5 units that starts 3 units below the quota goes 2 units over — and pays for 2."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.usage import record_usage

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await record_usage(ts, capability_id="ai.email_draft", quantity=17,
                           idempotency_key="pre")   # 3 left of 20

        res = await check_and_meter(
            ts, capability_id="ai.email_draft", quantity=5, idempotency_key="over"
        )
        assert res.allowed is True
        # 2 units over x 2 credits/unit. The 3 units inside the quota are already paid for.
        assert await balance(ts) == 1000 - 4
