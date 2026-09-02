# tests/test_billing_rating.py
from __future__ import annotations

from nexus.billing.rating import CREDIT_CENTS

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


async def test_a_credit_priced_action_produces_no_invoice_line():
    """The customer already paid for it, in credits, at the moment of use.

    `rate_period` used to charge everything past the plan's included quota as an `overage` line.
    With one price per request paid from the balance, doing that as well bills the same action
    twice — which is exactly the Stripe-observed double-charge that motivated moving to a single
    price. So rating now invoices GAUGES ONLY: seats and stored bytes are levels rather than
    requests, they never draw on the balance, and they are the only thing left with a period figure
    an invoice can price.
    """
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "ai.email_draft", 30, key="d1")
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        over = [ln for ln in await _lines(ts, inv) if ln.kind == "overage"]
    assert over == [], (
        "a request-priced capability reached the invoice; the balance was already debited for it"
    )


async def test_a_gauge_beyond_its_quota_is_still_invoiced():
    """The other half, and the reason rating did not simply become a no-op.

    Stored gigabytes are a LEVEL, not a request. Nothing burns credits for them — there is no
    "storage request" to price at the seam — so a workspace holding more than its plan allows has a
    real charge that only the invoice can carry. Had the previous test been implemented by deleting
    the overage line outright, going over a storage or seat cap would have become free.

    `platform.storage` rather than `seat.member` because seats are deliberately UNPRICED in credits
    (`rates.UNPRICED_BY_DESIGN` — they carry a seat price instead, and pricing them twice would
    double-charge). Storage is the gauge that actually carries both a quota and a rate card.
    """
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _setup("free")          # platform.storage quota 1 GB, card 25 credits/GB
    key = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await _use(ts, "platform.storage", 4, key="s1")     # 3 GB over
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=key)
        over = [ln for ln in await _lines(ts, inv) if ln.kind == "overage"]
    assert len(over) == 1, "a gauge past its quota must still reach the invoice"
    assert float(over[0].quantity) == 3
    assert over[0].amount_cents > 0


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


async def test_overage_never_undercuts_the_cheapest_in_plan_rate():
    """The ladder must not invert at its one escape hatch.

    Overage was priced at 1 credit = 1 cent while in-plan credits sell for 2.48c (Scale Annual) to
    4.75c (Core). Exceeding your plan was therefore **two to five times cheaper per credit than
    upgrading to cover the same usage**, so a customer acting rationally would sit on the smallest
    plan and overflow forever, and the tier they were nominally on would stop meaning anything.

    This asserts the rule rather than the number: overage per credit must exceed what a credit
    costs on the most generous plan we sell.
    """
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rating import CREDIT_CENTS
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    await sync_catalog()
    await sync_plans()
    async with get_sessionmaker()() as s:
        plans = (await s.scalars(
            select(BillingPlan).where(BillingPlan.plan_class == "standard")
        )).all()

    rates = [
        p.base_price_cents / p.included_credits
        for p in plans
        if p.included_credits and p.base_price_cents
    ]
    assert rates, "expected some priced standard plans"
    best_in_plan = min(rates)          # cents per credit on the most generous tier
    assert CREDIT_CENTS > best_in_plan, (
        f"overage at {CREDIT_CENTS}c/credit undercuts the best in-plan rate of "
        f"{best_in_plan:.3f}c — a customer is better off overflowing than upgrading"
    )
