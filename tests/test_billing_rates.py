# tests/test_billing_rates.py
from __future__ import annotations


def test_money_models_registered():
    import nexus.models as m

    for n in ("BillingRateCard", "BillingCostRate", "BillingCreditLedger",
              "BillingInvoice", "BillingInvoiceLine"):
        assert hasattr(m, n), f"{n} not exported"


async def test_rate_card_and_cost_rate_round_trip():
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCostRate, BillingRateCard

    async with get_sessionmaker()() as s:
        s.add(BillingRateCard(capability_id="ai.email_draft", credits_per_unit=2,
                              tiers=[{"upto": 10000, "credits": 2},
                                     {"upto": None, "credits": 1}]))
        s.add(BillingCostRate(capability_id="ai.email_draft", unit_cost_usd=0.0012,
                              source="groq llama-3.3-70b"))
        await s.commit()

    async with get_sessionmaker()() as s:
        rc = await s.get(BillingRateCard, "ai.email_draft")
        assert rc.credits_per_unit == 2 and rc.tiers[1]["credits"] == 1
        cr = await s.get(BillingCostRate, "ai.email_draft")
        assert float(cr.unit_cost_usd) == 0.0012


async def test_seed_rates_creates_cards_and_costs():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import RATE_SEED, sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCostRate, BillingRateCard
    from sqlalchemy import func, select

    await sync_catalog()
    res = await sync_rates()
    assert res["rate_cards"] == len(RATE_SEED)
    assert (await sync_rates())["rate_cards"] == 0        # idempotent

    async with get_sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(BillingRateCard)) == len(RATE_SEED)
        assert await s.scalar(select(func.count()).select_from(BillingCostRate)) > 0


def test_every_seeded_rate_clears_the_margin_floor():
    """The 50% gross-margin floor is a property of the seed, verified in CI — not a wish."""
    from nexus.billing.rates import RATE_SEED, gross_margin

    for r in RATE_SEED:
        m = gross_margin(r["credits_per_unit"], r["unit_cost_usd"])
        assert m >= 0.50, f"{r['capability_id']} margin {m:.2%} below the 50% floor"


def test_gross_margin_math():
    from nexus.billing.rates import gross_margin

    # 2 credits = $0.02 revenue, $0.0012 cost -> 94%
    assert round(gross_margin(2, 0.0012), 2) == 0.94
    assert gross_margin(0, 0.01) == 0.0           # free capability -> no margin
    assert gross_margin(5, 0) == 1.0              # zero COGS -> 100%


async def test_validate_rate_rejects_below_floor():
    from nexus.billing.rates import MarginFloorError, validate_rate

    # 1 credit ($0.01) against $0.008 COGS = 20% -> must be refused
    try:
        validate_rate("ai.account_qa", credits_per_unit=1, unit_cost_usd=0.008)
    except MarginFloorError as exc:
        assert "50" in str(exc) or "margin" in str(exc).lower()
        return
    raise AssertionError("validate_rate must reject a below-floor rate")


async def test_validate_rate_allows_explicit_exception():
    from nexus.billing.rates import validate_rate

    # Finance may override, but must say so explicitly — it becomes visible on the dashboard.
    validate_rate("ai.account_qa", credits_per_unit=1, unit_cost_usd=0.008,
                  margin_exception=True)


async def test_every_capability_is_priced_or_explicitly_exempt(fresh_db):
    """A capability with no rate card is metered and then **rated at nothing**.

    That is worse than being unmetered, because usage events accumulate and quotas count down, so
    it looks handled. Found by audit on 2026-08-25: 33 capabilities had no rate card, and the
    largest was `ai.scoring` — 4,090 runs, 98% of all agent activity, metered at the call site and
    free.

    Exemptions must be named in `UNPRICED_BY_DESIGN` with a reason, so a new capability cannot join
    the list by being forgotten.
    """
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import UNPRICED_BY_DESIGN, sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability, BillingRateCard

    await sync_catalog()
    await sync_rates()
    async with get_sessionmaker()() as s:
        caps = {c.id for c in (await s.scalars(select(BillingCapability))).all()}
        rated = {r.capability_id for r in (await s.scalars(select(BillingRateCard))).all()}

    # Module gates are on/off entitlements, not units of anything.
    priceable = {c for c in caps if not c.startswith("module.")}
    missing = priceable - rated - UNPRICED_BY_DESIGN
    assert not missing, (
        f"{sorted(missing)} are metered but have no price, so they rate to nothing. "
        f"Add a rate card, or name them in UNPRICED_BY_DESIGN with a reason."
    )


async def test_the_ai_workload_that_dominates_runs_is_priced(fresh_db):
    """`ai.scoring` specifically, because it is the one that got missed and it is 98% of runs.

    Priced low per unit on purpose — it runs on every account on every refresh, so it must not
    dominate a bill. It is the volume that makes it material, not the unit.
    """
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingRateCard

    await sync_catalog()
    await sync_rates()
    async with get_sessionmaker()() as s:
        row = (await s.scalars(
            select(BillingRateCard).where(BillingRateCard.capability_id == "ai.scoring")
        )).first()
    assert row is not None, "ai.scoring is metered on every account refresh and must be priced"
    assert float(row.credits_per_unit) > 0


async def test_token_metered_ai_exists_for_unbounded_work(fresh_db):
    """Flat per-action pricing is right for a predictable bill, and measured token spread is only
    ~4x median-to-max within an agent, which these margins absorb. `ai.tokens` exists for the cases
    where that stops being true — long-context work a customer can make arbitrarily large."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingRateCard

    await sync_catalog()
    await sync_rates()
    async with get_sessionmaker()() as s:
        row = (await s.scalars(
            select(BillingRateCard).where(BillingRateCard.capability_id == "ai.tokens")
        )).first()
    assert row is not None
