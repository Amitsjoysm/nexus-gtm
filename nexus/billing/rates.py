# nexus/billing/rates.py
"""Rate cards (what we charge) and cost rates (what it costs us), plus the margin guardrail.

The ≥50% gross-margin floor from docs/billing/11-Profitability-Analysis.md is enforced HERE, as
a validation rule: a rate that would ship underwater is refused unless finance records an
explicit exception. That makes the margin target structural rather than aspirational.

Prices are in credits (1 credit = $0.01 list). Costs are USD per unit, sourced from
docs/billing/12-Cost-Analysis.md §2.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCostRate, BillingRateCard

logger = logging.getLogger("nexus.billing.rates")

CREDIT_USD = 0.01
MIN_GROSS_MARGIN = 0.50


class MarginFloorError(ValueError):
    """Raised when a rate would ship below the gross-margin floor without an exception."""


def gross_margin(credits_per_unit: float, unit_cost_usd: float) -> float:
    """(revenue - cost) / revenue for one unit. 0.0 when the capability is free."""
    revenue = float(credits_per_unit) * CREDIT_USD
    if revenue <= 0:
        return 0.0
    return max(0.0, (revenue - float(unit_cost_usd)) / revenue)


def validate_rate(
    capability_id: str, *, credits_per_unit: float, unit_cost_usd: float,
    margin_exception: bool = False,
) -> float:
    """Return the margin, or raise if it is below floor without an explicit exception."""
    margin = gross_margin(credits_per_unit, unit_cost_usd)
    if margin < MIN_GROSS_MARGIN and not margin_exception:
        raise MarginFloorError(
            f"{capability_id}: {margin:.1%} gross margin is below the "
            f"{MIN_GROSS_MARGIN:.0%} floor (price {credits_per_unit} credits vs "
            f"${unit_cost_usd} cost). Reprice, or record a margin exception."
        )
    return margin


def _r(capability_id: str, credits: float, cost: float, source: str = "",
       tiers: list | None = None) -> dict:
    return {
        "capability_id": capability_id, "credits_per_unit": credits,
        "unit_cost_usd": cost, "source": source, "tiers": tiers or [],
    }


# Launch rate card (docs/billing/13-Pricing-Recommendations.md §2). Every line clears 50%.
RATE_SEED: list[dict] = [
    _r("ai.email_draft", 2, 0.0012, "groq llama-3.3-70b"),
    _r("outreach.cadence_touch", 2, 0.0013, "groq + smtp"),
    _r("ai.account_qa", 3, 0.012, "groq + 2 web searches"),
    _r("ai.research_brief", 3, 0.012, "exa research + groq"),
    _r("ai.call_script", 2, 0.0016, "groq"),
    _r("ai.contact_rank", 1, 0.0009, "groq"),
    _r("ai.chat_turn", 1, 0.0010, "groq budgeted envelope"),
    _r("ai.icp_from_website", 5, 0.010, "crawl + groq"),
    _r("ai.personalization_fetch", 8, 0.030, "apify actor"),
    _r("enrich.phone", 8, 0.030, "apify actor run"),
    _r("discovery.account_added", 5, 0.015, "exa pool + enrich amortized"),
    _r("discovery.lookalike_company", 25, 0.10, "search + enrich + llm"),
    _r("discovery.lookalike_contact", 2, 0.0005, "in-workspace scoring"),
    _r("enrich.account", 3, 0.010, "crawl + llm"),
    _r("enrich.contact", 4, 0.012, "search + finder + verify"),
    _r("enrich.source_committee", 15, 0.05, "search + llm + verifies"),
    _r("enrich.linkedin_finder", 2, 0.004, "search"),
    _r("verify.email", 0.25, 0.0002, "reacher self-hosted"),
    _r("outreach.email_send", 1, 0.0001, "customer smtp"),
    _r("outreach.email_draft_save", 1, 0.0001, "customer imap"),
    _r("outreach.sep_push", 0.5, 0.0001, "customer sep account"),
    _r("integration.crm_sync", 0.5, 0.0001, "customer crm account"),
    _r("network.source_sync", 2, 0.002, "google/microsoft graph"),
    _r("network.search", 0.5, 0.0002, "indexed sql"),
    _r("network.linkedin_import", 5, 0.0005, "csv parse"),
    _r("calling.brief", 2, 0.001, "assembled dossier"),
    _r("calling.minutes", 4, 0.014, "twilio"),
    _r("notify.webhook", 0.1, 0.0001, "http post"),
    _r("notify.slack", 0.1, 0.0001, "http post"),
    _r("notify.email_digest", 0.5, 0.0001, "smtp"),
    _r("data.export", 5, 0.001, "compute"),
    _r("data.import_csv", 2, 0.0005, "compute"),
    _r("workflow.orchestration_run", 5, 0.005, "multi-step tools"),
    _r("automation.play_run", 1, 0.0002, "compute"),
    _r("platform.storage", 25, 0.10, "postgres gb-month"),
    _r("search.web", 1, 0.004, "exa/brave/serper blended"),
    _r("signal.news_scan", 1, 0.004, "search"),

    # ---- added 2026-08-25, after an audit found 33 capabilities with no rate card ------------
    #
    # A capability with no rate card is metered and then **rated at nothing**. It looks billed —
    # usage events accumulate, quotas count down — and produces no revenue line. That is worse
    # than being unmetered, because the usage data makes it look handled.
    #
    # The largest gap by far: `ai.scoring` had run **4,090 times, 98% of all agent activity**,
    # metered at the call site, and free. It is priced low per unit on purpose (0.5 credits) —
    # it runs on every account on every refresh, so it has to be cheap enough not to dominate a
    # bill, and it is the volume that makes it material rather than the unit.
    _r("ai.scoring", 0.5, 0.00022, "groq; median 226 tokens over 4,090 measured runs"),

    # **Token-metered AI.** The other capabilities price a whole action at a flat rate, which is
    # right for a predictable bill: measured token spread within one agent runs to 49x
    # min-to-max, but only ~4x median-to-max, and at these margins a 4x outlier is absorbed. This
    # exists for the cases where that stops being true — long-context work, document ingestion,
    # anything a customer can make arbitrarily large. Priced per 1,000 tokens.
    _r("ai.tokens", 0.01, 0.0000030, "per 1k tokens, model-blended"),

    # A frontier model costs an order of magnitude more than the default. Charging the same for
    # both would mean the customers who ask for the better model are subsidised by the ones who
    # do not.
    _r("ai.premium_model", 4, 0.012, "frontier model, per call"),

    # Makes `module.api` sellable as a metered product rather than an on/off flag.
    _r("api.request", 0.1, 0.00004, "served API request"),

    _r("signal.stored", 0.25, 0.0006, "dedupe + classify + store"),
    _r("signal.rss_scan", 0.5, 0.0015, "feed fetch + parse"),
    _r("inbox.task", 0.1, 0.00004, "compute"),
    _r("network.intro_paths", 2, 0.0006, "graph traversal"),
    _r("network.persons", 0.5, 0.00015, "resolution + store"),
    _r("notify.in_app", 0.05, 0.00002, "compute"),
    _r("outreach.campaign", 5, 0.010, "orchestration + sends"),
    _r("report.analytics", 1, 0.0025, "aggregate queries"),
    _r("report.cadence", 1, 0.0025, "aggregate queries"),
    _r("calling.task", 0.5, 0.0004, "queue + disposition"),
    _r("discovery.icp_daily", 10, 0.030, "daily net-new sweep"),
    _r("workflow.orchestration_step", 1, 0.0010, "one tool step"),
    _r("integration.crm_connection", 1, 0.0020, "oauth + verify"),
    _r("automation.account_refresh", 0.5, 0.0012, "crawl, amortized"),
]

# Deliberately unpriced, and it is not an oversight — a rate card here would be wrong:
#
# * `seat.member` is billed as a SEAT PRICE, not in credits. Pricing it twice would double-charge.
# * `platform.workspace`, `platform.custom_fields` have no marginal cost; they are structural
#   limits enforced by quota, and a quota needs no price.
# * `job.queue_execution` is internal plumbing. Billing a customer for our own retry is indefensible.
# * `module.*` gates are on/off entitlements, not units of anything.
#
# Pinned by `test_every_capability_is_priced_or_explicitly_exempt`, so a NEW capability cannot
# quietly join this list by being forgotten.
UNPRICED_BY_DESIGN: frozenset[str] = frozenset({
    "seat.member",
    "platform.workspace",
    "platform.custom_fields",
    "job.queue_execution",
})


async def sync_rates() -> dict:
    """Seed rate cards + cost rates for capabilities that have none. Never overwrites an
    existing rate: once live, pricing is owned by Admin, not by a redeploy."""
    created_rc = created_cr = 0
    async with get_sessionmaker()() as session:
        have_rc = {r.capability_id for r in (await session.scalars(select(BillingRateCard))).all()}
        have_cr = {r.capability_id for r in (await session.scalars(select(BillingCostRate))).all()}
        for spec in RATE_SEED:
            cid = spec["capability_id"]
            # Guardrail runs on the seed itself, so a bad price can never reach the database.
            validate_rate(
                cid, credits_per_unit=spec["credits_per_unit"],
                unit_cost_usd=spec["unit_cost_usd"],
            )
            if cid not in have_rc:
                session.add(BillingRateCard(
                    capability_id=cid, credits_per_unit=spec["credits_per_unit"],
                    tiers=spec["tiers"],
                ))
                created_rc += 1
            if cid not in have_cr:
                session.add(BillingCostRate(
                    capability_id=cid, unit_cost_usd=spec["unit_cost_usd"],
                    source=spec["source"],
                ))
                created_cr += 1
        await session.commit()
    if created_rc or created_cr:
        logger.info("rate sync: %d cards, %d cost rates created", created_rc, created_cr)
    return {"rate_cards": created_rc, "cost_rates": created_cr}
