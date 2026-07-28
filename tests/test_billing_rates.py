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
