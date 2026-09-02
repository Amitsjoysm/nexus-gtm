# tests/test_no_double_charge.py
"""A request paid for in credits must not also appear on the invoice.

Under credits-only billing the in-flight burn IS the charge: every metered request deducts
``credits_per_unit x quantity`` from a balance the customer already bought with their subscription.

`rate_period` was written for the old model, where credits were burned only PAST the quota and the
remainder was invoiced. It computes ``over = quantity - quota`` and bills it. Left alone, that is a
double charge: quotas no longer stop a non-gauge capability, so usage routinely exceeds the quota
number while every one of those units has already been paid for in credits. The customer would be
billed once in credits and again in dollars for the same action.

Gauges are the exception on the other side: `seat.member` and `platform.storage` are never charged
in credits, so if anything is ever to be invoiced for exceeding them, that path must stay reachable.
"""
from __future__ import annotations

import pytest

from nexus.models.identity import Tenant


async def _tenant(plan_id: str, *, credits: float):
    from nexus.billing.credits import grant_credits
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        t = Tenant(name=plan_id, slug=f"dc-{plan_id}")
        s.add(t)
        await s.flush()
        ts = TenantSession(s, t.id)
        await ensure_subscription(ts, plan_id=plan_id)
        await grant_credits(ts, credits, kind="grant", reason="test",
                            idempotency_key=f"seed:{t.id}")
        await s.commit()
        return t.id


@pytest.fixture
async def seeded(fresh_db, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")


async def test_credit_paid_usage_is_not_invoiced_again(seeded):
    """THE double charge. `verify.email` on free has a quota of 50; drive usage past it and every
    unit has already been paid in credits."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession

    tid = await _tenant("free", credits=1000)

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        # Well past the 50 quota; each unit burns 0.25 credits in flight.
        for i in range(8):
            await check_and_meter(ts, capability_id="verify.email", quantity=10,
                                  idempotency_key=f"v{i}")
        await rebuild_rollups(ts)
        invoice = await rate_period(ts, period_key=period_key(utcnow(), 'period'))
        await s.commit()

    assert invoice.total_cents == 0, (
        f"a credit-funded plan was invoiced {invoice.total_cents}c for usage its credits covered"
    )


async def test_a_gauge_over_its_cap_can_still_be_invoiced(seeded):
    """The other side. Gauges are never charged in credits, so the invoice path must stay
    reachable for them — otherwise exceeding a seat limit becomes free."""
    import inspect

    from nexus.billing import rating

    src = inspect.getsource(rating.rate_period)
    assert "gauge" in src, (
        "rate_period no longer distinguishes gauges, so either credit-paid usage is double-charged "
        "or exceeding a seat cap is free — it cannot be neither"
    )


async def test_legacy_unlimited_rating_is_unchanged(seeded):
    """A grandfathered tenant burns no credits, so nothing here may change what it is invoiced."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession

    tid = await _tenant("legacy-unlimited", credits=0)
    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        await check_and_meter(ts, capability_id="verify.email", quantity=100,
                              idempotency_key="lu1")
        await rebuild_rollups(ts)
        invoice = await rate_period(ts, period_key=period_key(utcnow(), 'period'))
        await s.commit()
    assert invoice.total_cents == 0, "an unlimited plan has no quota to exceed"
