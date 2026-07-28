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


# ---- volume ladder ---------------------------------------------------------------------------
# Every RATE_SEED line currently ships `tiers: []`, so the rating tests above all take the flat
# branch and leave the ladder unverified. Admin exposes tier editing in M6, so it gets pinned
# here first.

def _card(credits, tiers):
    from nexus.models.billing import BillingRateCard

    return BillingRateCard(capability_id="x", credits_per_unit=credits, tiers=tiers)


def test_tiered_credits_falls_back_to_the_flat_rate_without_tiers():
    from nexus.billing.rating import tiered_credits

    assert tiered_credits(100, _card(2, [])) == 200


def test_tiered_credits_prices_each_band_at_its_own_rate():
    from nexus.billing.rating import tiered_credits

    card = _card(3, [{"upto": 100, "credits": 3}, {"upto": 1000, "credits": 2}])
    assert tiered_credits(50, card) == 150                 # wholly inside band 1
    assert tiered_credits(100, card) == 300                # exactly fills band 1
    # 100 @ 3 + 150 @ 2 -- the ladder is marginal, not a cliff that reprices earlier units.
    assert tiered_credits(250, card) == 300 + 300


def test_tiered_credits_catch_all_band_absorbs_the_remainder():
    from nexus.billing.rating import tiered_credits

    card = _card(5, [{"upto": 10, "credits": 5}, {"upto": None, "credits": 1}])
    assert tiered_credits(10, card) == 50
    assert tiered_credits(1_000_000, card) == 50 + 999_990


def test_tiered_credits_charges_the_base_rate_when_the_ladder_runs_out():
    """A ladder with no catch-all band must not silently make the tail free."""
    from nexus.billing.rating import tiered_credits

    card = _card(7, [{"upto": 10, "credits": 1}])          # covers only the first 10
    assert tiered_credits(10, card) == 10
    assert tiered_credits(12, card) == 10 + 14             # 2 units at the base rate of 7


def test_tiered_credits_of_zero_units_is_free():
    from nexus.billing.rating import tiered_credits

    assert tiered_credits(0, _card(3, [{"upto": 10, "credits": 3}])) == 0


async def test_rating_picks_the_subscription_deterministically():
    """Re-rating must reproduce the same invoice even if a tenant briefly holds two active
    subscriptions — otherwise the replayability guarantee is only true when data is tidy."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription

    tid = await _setup("free")                      # base 0
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active"))   # base 7900, newer
        await ts.flush()
        await rebuild_rollups(ts)

        first = await rate_period(ts, period_key=key)
        second = await rate_period(ts, period_key=key)
        assert first.total_cents == second.total_cents
        assert first.plan_id == second.plan_id
