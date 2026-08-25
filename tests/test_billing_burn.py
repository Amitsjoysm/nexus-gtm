# tests/test_billing_burn.py
"""Credits are actually spent.

Before this, `burn_credits()` was implemented, tested, and called by nothing, while
`grant_credits()` ran on every period roll — so a balance only ever grew and the ledger was an
accounting fiction. The burn order is docs/billing/04-Pricing-Engine.md §2: included quota
first, then credits, then an explicit overage price, then the wall.
"""
from __future__ import annotations

from nexus.billing.rating import CREDIT_CENTS

import pytest

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "free"):
    """A tenant on `plan_id`. Free gives ai.email_draft a quota of 20."""
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


async def _burn_to(ts, capability: str, units: int):
    """Consume `units` of quota directly, bypassing the seam."""
    from nexus.billing.usage import record_usage

    await record_usage(ts, capability_id=capability, quantity=units,
                       idempotency_key=f"pre:{capability}:{units}")


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


async def test_inside_quota_burns_nothing(enforcing):
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="a")
        assert res.allowed is True
        # Quota is what they already paid for; using it must not also spend credits.
        assert await balance(ts) == 1000


async def test_overage_spends_credits_and_allows(enforcing):
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()          # ai.email_draft quota 20, rate card 2 credits/unit
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await _burn_to(ts, "ai.email_draft", 20)          # exactly at quota

        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="over1")
        assert res.allowed is True                         # credits carried it
        assert await balance(ts) == 1000 - 2               # 1 unit over x 2 credits


async def test_overage_without_credits_blocks_when_enforcing(enforcing):
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await _burn_to(ts, "ai.email_draft", 20)
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="over1")
        assert res.allowed is False
        assert res.reason == "quota_exhausted"
        assert await balance(ts) == 0                      # nothing spent on a refused action


async def test_overage_price_allows_without_credits(enforcing):
    """An overage price means "keep going and invoice it", not "stop"."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.credits import balance

    tid = await _seed("growth")     # verify.email quota 5000, overage_price_credits 1
    async with tenant_session(tid) as ts:
        await _burn_to(ts, "verify.email", 5000)
        res = await check_and_meter(ts, capability_id="verify.email", idempotency_key="v1")
        assert res.allowed is True
        # No balance to draw on, so it goes on the invoice rather than the ledger.
        assert await balance(ts) == 0


async def test_burn_is_idempotent_across_retries(enforcing):
    """A retried request re-derives the same overage; it must not be charged twice, and it must
    not be refused for an action it already paid for."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 1000, reason="x", idempotency_key="g")
        await _burn_to(ts, "ai.email_draft", 20)

        first = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="same")
        after_first = await balance(ts)
        second = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="same")

        assert first.allowed is True and second.allowed is True
        assert after_first == 1000 - 2
        assert await balance(ts) == after_first        # charged exactly once


async def test_plan_overage_price_beats_the_rate_card(enforcing):
    """Growth prices verify.email overage at 1 credit; the global card says 0.25."""
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 500, reason="x", idempotency_key="g")
        await _burn_to(ts, "verify.email", 5000)
        await check_and_meter(ts, capability_id="verify.email", quantity=10,
                              idempotency_key="v1")
        assert await balance(ts) == 500 - 10          # 10 x 1 credit, not 10 x 0.25


async def test_a_failing_burn_degrades_to_allow(monkeypatch, enforcing):
    """The seam must never break the product, even when the money path errors."""
    import nexus.billing.entitlements as ent_mod
    from nexus.billing.entitlements import check_and_meter

    async def boom(*a, **k):
        raise RuntimeError("ledger down")

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        await _burn_to(ts, "verify.email", 5000)
        monkeypatch.setattr(ent_mod, "_burn_for_overage", boom)
        res = await check_and_meter(ts, capability_id="verify.email", idempotency_key="v1")
        # Whole seam is wrapped: an internal failure resolves to allow, never to a 402.
        assert res.allowed is True


async def test_shadow_mode_still_never_blocks():
    """Default posture is unchanged: evaluate and record, never refuse."""
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await _burn_to(ts, "ai.email_draft", 50)      # far past the quota of 20
        res = await check_and_meter(ts, capability_id="ai.email_draft", idempotency_key="s1")
        assert res.allowed is True
        assert res.would_block is True


async def test_credits_and_invoice_do_not_both_charge_for_the_same_overage(enforcing):
    """Regression: the customer must not pay twice for one overage.

    Found by running the whole loop against Stripe — 200 units past quota burned 200 credits at
    the moment of use AND appeared as a 200c overage line on the invoice. Credits are pre-paid,
    so whatever they covered must not be billed again.
    """
    from nexus.billing.credits import balance, grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoiceLine

    tid = await _seed("growth")            # verify.email quota 5000, overage 1 credit/unit
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 3000, reason="included", idempotency_key="g")
        await check_and_meter(ts, capability_id="verify.email", quantity=5200,
                              idempotency_key="bulk")

        # 200 units over -> 200 credits spent at point of use.
        assert await balance(ts) == 2800

        await rebuild_rollups(ts)
        key = period_key(utcnow(), "period")
        inv = await rate_period(ts, period_key=key)
        lines = await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)
        overage = [ln for ln in lines if ln.kind == "overage"]

        # The overage was already paid from the balance, so it must not be on the invoice too.
        assert overage == []
        assert inv.total_cents == 7900          # base fee only


async def test_overage_beyond_the_credit_balance_still_reaches_the_invoice(enforcing):
    """Only the part credits actually covered is deducted; the rest is still billable."""
    from nexus.billing.credits import grant_credits
    from nexus.billing.entitlements import check_and_meter
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoiceLine

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 50, reason="small", idempotency_key="g")
        # 200 over, but only 50 credits exist -> the burn is refused (all-or-nothing), so the
        # whole overage goes on the invoice.
        await check_and_meter(ts, capability_id="verify.email", quantity=5200,
                              idempotency_key="bulk")
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        lines = await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)
        overage = [ln for ln in lines if ln.kind == "overage"]
        assert len(overage) == 1
        # 200 units x 1 credit x CREDIT_CENTS (5c). Overage is priced above the dearest in-plan
        # rate so that upgrading always beats overflowing.
        assert overage[0].amount_cents == 200 * CREDIT_CENTS
