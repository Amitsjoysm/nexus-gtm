# tests/test_billing_rating.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _setup(plan_id="growth"):
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


async def _use(ts, cap, qty, *, key):
    from nexus.billing.usage import record_usage

    await record_usage(ts, capability_id=cap, quantity=qty, idempotency_key=key)


async def _lines(ts, inv):
    from nexus.models.billing import BillingInvoiceLine

    return await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)


async def test_rate_period_charges_base_fee_only_when_no_overage():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("growth")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "verify.email", 10, key="v1")      # far under the 5000 quota
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        assert inv.status == "draft"
        kinds = {ln.kind for ln in await _lines(ts, inv)}
        assert "base" in kinds
        assert "overage" not in kinds
        assert inv.total_cents == 7900                      # Growth base fee only


async def test_rate_period_charges_overage_beyond_quota():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")     # Free: ai.email_draft quota 20
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 30, key="d1")     # 10 over
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        over = [ln for ln in await _lines(ts, inv) if ln.kind == "overage"]
        assert len(over) == 1
        assert float(over[0].quantity) == 10
        # 10 units x 2 credits x $0.01 = $0.20 = 20 cents
        assert over[0].amount_cents == 20


async def test_plan_overage_price_overrides_the_rate_card():
    """Growth prices verify.email overage at 1 credit/unit; the global card says 0.25.

    The plan entitlement must win, otherwise a negotiated rate would silently bill at list.
    """
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("growth")     # verify.email quota 5000, overage_price_credits 1
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "verify.email", 5100, key="v1")     # 100 over
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        over = [ln for ln in await _lines(ts, inv) if ln.kind == "overage"]
        assert len(over) == 1
        assert float(over[0].unit_credits) == 1
        assert over[0].amount_cents == 100                 # 100 x 1 credit, NOT 100 x 0.25
        assert inv.total_cents == 7900 + 100               # base + overage


async def test_rating_is_deterministic_and_replayable():
    """Re-rating a period must reproduce identical lines — the audit guarantee."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 25, key="d1")
        await rebuild_rollups(ts)
        first = await rate_period(ts, period_key=key)
        first_total = first.total_cents
        first_lines = sorted((ln.kind, float(ln.quantity), ln.amount_cents)
                             for ln in await _lines(ts, first))

        second = await rate_period(ts, period_key=key)   # re-rate the same period
        assert second.id == first.id                      # upserted, not duplicated
        assert second.total_cents == first_total
        second_lines = sorted((ln.kind, float(ln.quantity), ln.amount_cents)
                              for ln in await _lines(ts, second))
        assert second_lines == first_lines


async def test_unlimited_plan_is_never_charged_overage():
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("legacy-unlimited")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 50_000, key="huge")
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        assert inv.total_cents == 0                       # $0 plan, no overage, ever
        assert [ln for ln in await _lines(ts, inv) if ln.kind == "overage"] == []


async def test_finalize_makes_invoice_immutable():
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("growth")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        finalized = await finalize_invoice(ts, inv.id)
        assert finalized.status == "finalized"
        assert finalized.number.startswith("INV-")
        assert finalized.finalized_at is not None

        # Re-rating a finalized period must NOT silently rewrite history.
        again = await rate_period(ts, period_key=key)
        assert again.status == "finalized"
        assert again.total_cents == finalized.total_cents
