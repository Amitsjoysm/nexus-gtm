"""Prospector's cost guards (decisions D11, D21, D22): a per-person rate limit and a per-workspace
daily row budget.

Browsing is free to the customer and paid per row by us, so these bound what one person or one
workspace can run up. The budget is enforced whatever billing enforcement says: it is a cost guard,
not a billing rule, and `off` records nothing. A superadmin raises it for a customer by setting a
quota on `prospector.daily_rows` in the plan (or custom plan) editor.
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from nexus.prospector.errors import ProspectorError
from tests.conftest import make_tenant, tenant_session


async def test_the_rate_limit_refuses_with_retry_after(monkeypatch):
    from nexus.prospector.limits import check_rate

    monkeypatch.setattr(get_settings(), "prospector_rate_limit_per_minute", 3)
    for _ in range(3):
        await check_rate("t1", "u1")
    with pytest.raises(ProspectorError) as err:
        await check_rate("t1", "u1")
    assert err.value.status == 429 and int(err.value.headers["Retry-After"]) >= 1
    await check_rate("t1", "u2")       # per person
    await check_rate("t2", "u1")       # and per workspace


async def test_rows_are_counted_per_workspace_per_day():
    from nexus.prospector.limits import count_rows, rows_today

    tid = await make_tenant("lim1")
    async with tenant_session(tid) as ts:
        await count_rows(ts, 25)
        await count_rows(ts, 50)
    async with tenant_session(tid) as ts:
        assert await rows_today(ts) == 75
    other = await make_tenant("lim2")
    async with tenant_session(other) as ts:
        assert await rows_today(ts) == 0


async def test_the_budget_refuses_a_page_that_would_cross_it(monkeypatch):
    from nexus.prospector.limits import count_rows, ensure_budget

    monkeypatch.setattr(get_settings(), "prospector_daily_rows_default", 2500)
    tid = await make_tenant("lim3")
    async with tenant_session(tid) as ts:
        await count_rows(ts, 2490)
        assert await ensure_budget(ts, 10) == 10
        with pytest.raises(ProspectorError) as err:
            await ensure_budget(ts, 25)
    assert err.value.status == 429
    assert "2,500" in err.value.detail and "00:00 UTC" in err.value.detail


async def test_the_budget_holds_with_billing_enforcement_off(monkeypatch):
    from nexus.prospector.limits import count_rows, ensure_budget

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    monkeypatch.setattr(get_settings(), "prospector_daily_rows_default", 100)
    tid = await make_tenant("lim4")
    async with tenant_session(tid) as ts:
        await count_rows(ts, 100)
        with pytest.raises(ProspectorError):
            await ensure_budget(ts, 1)


async def test_a_plan_quota_raises_the_daily_limit(monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingPlanEntitlement, BillingSubscription
    from nexus.prospector.limits import daily_limit

    monkeypatch.setattr(get_settings(), "prospector_daily_rows_default", 2500)
    await sync_catalog()
    await sync_plans()
    tid = await make_tenant("lim5")
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="launch", status="active"))
        await ts.flush()
        assert await daily_limit(ts) == 2500
        ts.session.add(BillingPlanEntitlement(plan_id="launch", capability_id="prospector.daily_rows",
                                              mode="enabled", quota=10000))
        await ts.flush()
        assert await daily_limit(ts) == 10000
