# nexus/billing/plans.py
"""Launch plan seed (docs/billing/13-Pricing-Recommendations.md §1).

Seeds are a STARTING POINT, not the source of truth: once a plan exists, the Admin portal owns
it. ``sync_plans`` therefore only creates missing plans and never overwrites an existing one —
a redeploy must not silently reprice live customers.

``legacy-unlimited`` is the migration keystone: every pre-billing tenant is attached to it so
the platform ships with zero behavioral change (docs/billing/15-Migration-Strategy.md §1).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingPlan, BillingPlanEntitlement

logger = logging.getLogger("nexus.billing.plans")

LEGACY_PLAN_ID = "legacy-unlimited"

# Per-plan capability policy. Only capabilities that differ from "allow" need a row; the
# entitlement engine falls back to the catalog default for anything unlisted.
#   (capability_id, mode, quota, overage_price_credits)
_FREE_ENT = [
    ("module.outreach", "disabled", None, None),
    ("module.network", "disabled", None, None),
    ("module.calling", "disabled", None, None),
    ("module.discovery", "disabled", None, None),
    ("module.integrations", "disabled", None, None),
    ("verify.email", "metered", 50, None),
    ("ai.email_draft", "metered", 20, None),
    ("platform.storage", "metered", 1, None),
    ("seat.member", "metered", 1, None),
]
# Core: account and contact intelligence, and nothing else. The one plan whose shape is defined by
# what it EXCLUDES, so every module gate is listed explicitly rather than inherited — a reader
# pricing a deal should not have to diff this against the catalog to see what the customer gets.
#
# What is left is not a rump: Dashboard, Accounts, Contacts, Members, Settings and Billing carry no
# capability by design (see NAV_ITEMS), so they are present on every plan including this one.
#
# `module.relevance` and `module.lists` are deliberately NOT in this list. Relevance scoring feeds
# the score column on the Accounts page, which Core includes — gating it would sell a page with its
# most useful column blanked. Lists are saved views over accounts Core already has.
_CORE_ENT = [
    ("module.signals", "disabled", None, None),
    ("module.outreach", "disabled", None, None),
    ("module.calling", "disabled", None, None),
    ("module.network", "disabled", None, None),
    ("module.discovery", "disabled", None, None),
    ("module.integrations", "disabled", None, None),
    ("module.plays", "disabled", None, None),
    ("module.agents", "disabled", None, None),
    ("verify.email", "metered", 500, 1),
    ("enrich.contact", "metered", 500, 2),
    ("seat.member", "metered", 3, None),
    ("platform.storage", "metered", 1, 25),
]
_STARTER_ENT = [
    ("module.network", "disabled", None, None),
    ("module.calling", "disabled", None, None),
    ("discovery.account_added", "metered", 150, 5),
    ("verify.email", "metered", 1000, 1),
    ("seat.member", "metered", 5, None),
    ("platform.storage", "metered", 2, 25),
]
_GROWTH_ENT = [
    ("discovery.account_added", "metered", 600, 5),
    ("verify.email", "metered", 5000, 1),
    ("seat.member", "metered", 25, None),
    ("platform.storage", "metered", 10, 25),
    ("network.source_sync", "metered", 60, 2),
]
_PRO_ENT = [
    ("module.api", "enabled", None, None),
    ("discovery.account_added", "metered", 1500, 5),
    ("verify.email", "metered", 15000, 1),
    ("seat.member", "metered", 100, None),
    ("platform.storage", "metered", 25, 25),
]
_BUSINESS_ENT = [
    ("module.api", "enabled", None, None),
    ("ai.premium_model", "enabled", None, None),
    ("discovery.account_added", "metered", 3000, 5),
    ("verify.email", "metered", 40000, 1),
    ("seat.member", "metered", 250, None),
    ("platform.storage", "metered", 100, 25),
]

PLAN_SEED: list[dict] = [
    {
        "id": LEGACY_PLAN_ID, "name": "Legacy Unlimited", "plan_class": "unlimited",
        "status": "grandfathered", "base_price_cents": 0, "seat_price_cents": 0,
        "included_credits": 0, "max_seats": None, "sort_order": 999,
        "description": "Pre-billing tenants. Never billed, never limited.",
        "entitlements": [],
    },
    {
        # 1,000 credits, raised from 100 on 2026-08-26: the free tier now has to let someone try
        # every feature, and 100 credits is roughly forty enrichments — not enough to reach the
        # part of the product worth paying for. Costs $1.92 per fully-consuming user at blended
        # COGS, $4.00 worst case. That is the acquisition budget, and the number to watch if
        # signups outpace conversions.
        "id": "free", "name": "Free", "plan_class": "free", "status": "active",
        # 200 credits is a TRIAL of the whole product, not a usable free tier, and that is the
        # intent. At the worst-case $0.004/credit COGS a free workspace costs at most $0.80 to
        # serve, so the entire funnel is affordable at any signup volume we can realistically get.
        # It buys roughly 25 enriched accounts or 66 research briefs — enough to see the product
        # work on the buyer's own data, not enough to run a territory on.
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 200,
        "max_seats": 1, "sort_order": 10,
        "description": "Try every feature with 1,000 credits.",
        "entitlements": _FREE_ENT,
    },
    {
        "id": "trial", "name": "Trial", "plan_class": "trial", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 1000,
        "max_seats": 5, "trial_days": 14, "sort_order": 15,
        "description": "14-day full-feature trial.",
        "entitlements": _GROWTH_ENT,
    },
    {
        # `standard`, not `custom` — that is the whole point. Custom and enterprise plans are
        # refused by /billing/checkout and /billing/portal with a 409 (_reject_if_admin_managed),
        # so a bespoke per-tenant plan can never be bought self-serve. Core is on the price list.
        #
        # No Stripe object is created here and none is needed: create_checkout calls
        # ensure_plan_price on first purchase when `meta.price_id` is empty and caches the result,
        # so the price is minted by the first customer who buys it.
        #
        # Credits are 21% of the price, matching Starter (750/3900) rather than Growth's 25% — a
        # cheaper plan carries proportionally less usage. All of it is editable in Admin without a
        # redeploy: sync_plans only ever CREATES, so these numbers are a starting point and the
        # database wins from then on.
        "id": "core", "name": "Core", "plan_class": "standard", "status": "retired",
        "base_price_cents": 1900, "seat_price_cents": 1900, "included_credits": 400,
        "max_seats": 3, "sort_order": 18,
        "description": "Accounts and contacts, with enrichment. No signals, outreach or agents.",
        "entitlements": _CORE_ENT,
    },
    {
        "id": "starter", "name": "Starter", "plan_class": "standard", "status": "retired",
        "base_price_cents": 3900, "seat_price_cents": 3900, "included_credits": 750,
        "max_seats": 5, "sort_order": 20,
        "description": "For a first SDR running outbound.",
        "entitlements": _STARTER_ENT,
    },
    {
        "id": "growth", "name": "Growth", "plan_class": "standard", "status": "retired",
        "base_price_cents": 7900, "seat_price_cents": 7900, "included_credits": 2000,
        "max_seats": 25, "sort_order": 30,
        "description": "Full GTM stack for a growing team.",
        "entitlements": _GROWTH_ENT,
    },
    # Seeded as `retired`: the superseded tiers exist so their EXISTING subscribers keep resolving
# entitlements from a real plan row, but a fresh install must not offer nine tiers. `sync_plans`
# never mutates an existing plan, so this only affects new databases — a live deployment is moved
# by `scripts/restructure_plans.py`, which retires the same set and reports who is on each.

# ---- the ladder as of 2026-08-26 ------------------------------------------------------------
    # Collapsed from eight public tiers to three plus two annuals. The superseded rows are RETIRED
    # by `scripts/restructure_plans.py`, never deleted: `billing_subscriptions.plan_id` is a foreign
    # key, and entitlements resolve from the plan ROW — so a deleted plan is either a constraint
    # violation or a paying customer with no entitlements at all, which falls back to permissive
    # catalog defaults and hands them everything.
    #
    # Sized against measured cost: blended $0.00192/credit, worst case $0.00400. Launch runs at
    # 25.3 credits/$ and Accelerate at 40.2, against a 100 cr/$ design ceiling.
    {
        "id": "launch", "name": "Launch", "plan_class": "standard", "status": "active",
        "base_price_cents": 9900, "seat_price_cents": 9900, "included_credits": 2000,
        "max_seats": 25, "sort_order": 20, "interval": "month", "trial_days": 14,
        "description": "Everything you need to run signal-led outreach.",
        "entitlements": _GROWTH_ENT,
    },
    {
        # A separate row rather than a flag, because `interval` lives on the plan. Sorted beside its
        # monthly sibling by hand: the automatic position derives from price, and a yearly price
        # would put every annual plan at the bottom of the ladder.
        "id": "launch-annual", "name": "Launch (annual)", "plan_class": "standard",
        "status": "active", "base_price_cents": 95000, "seat_price_cents": 95000,
        # 12 x the monthly allowance exactly. The annual discount is taken on PRICE (20% off) and
        # deliberately not on credits: discounting both would compound into a cr/$ ratio well past
        # the design ceiling, and the customer is paying up front for commitment, not for a
        # cheaper unit of consumption.
        "included_credits": 24000, "max_seats": 25, "sort_order": 21,
        "interval": "year", "trial_days": 14,
        "description": "Launch, paid yearly. Two months free.",
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "accelerate", "name": "Accelerate", "plan_class": "standard", "status": "active",
        "base_price_cents": 19900, "seat_price_cents": 19900, "included_credits": 8000,
        "max_seats": 100, "sort_order": 30, "interval": "month", "trial_days": 14,
        "description": "For teams running the full loop at volume.",
        "entitlements": _PRO_ENT,
    },
    {
        "id": "accelerate-annual", "name": "Accelerate (annual)", "plan_class": "standard",
        "status": "active", "base_price_cents": 191000, "seat_price_cents": 191000,
        "included_credits": 96000, "max_seats": 100, "sort_order": 31,
        "interval": "year", "trial_days": 14,
        "description": "Accelerate, paid yearly. Two months free.",
        "entitlements": _PRO_ENT,
    },
    {
        "id": "professional", "name": "Professional", "plan_class": "standard",
        "status": "retired", "base_price_cents": 12900, "seat_price_cents": 12900,
        "included_credits": 4000, "max_seats": 100, "sort_order": 40,
        "description": "API access and priority support.",
        "entitlements": _PRO_ENT,
    },
    {
        "id": "business", "name": "Business", "plan_class": "standard", "status": "retired",
        "base_price_cents": 19900, "seat_price_cents": 19900, "included_credits": 8000,
        "max_seats": 250, "sort_order": 50,
        "description": "Scale outbound with advanced controls.",
        "entitlements": _BUSINESS_ENT,
    },
    {
        "id": "enterprise", "name": "Enterprise", "plan_class": "enterprise", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 0,
        "max_seats": None, "sort_order": 60,
        "description": "Custom contract; entitlements come from the contract.",
        "entitlements": [],
    },
    {
        "id": "internal", "name": "Internal", "plan_class": "internal", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 0,
        "max_seats": None, "sort_order": 900,
        "description": "Staff/demo workspaces. Metered, never billed.",
        "entitlements": [],
    },
]


async def sync_plans() -> dict:
    """Create any missing seed plans + their entitlements. Never mutates an existing plan."""
    created = 0
    async with get_sessionmaker()() as session:
        existing = {p.id for p in (await session.scalars(select(BillingPlan))).all()}
        for spec in PLAN_SEED:
            if spec["id"] in existing:
                continue
            data = {k: v for k, v in spec.items() if k != "entitlements"}
            session.add(BillingPlan(**data))
            await session.flush()
            for cap_id, mode, quota, overage in spec["entitlements"]:
                session.add(
                    BillingPlanEntitlement(
                        plan_id=spec["id"], capability_id=cap_id, mode=mode,
                        quota=quota, overage_price_credits=overage,
                    )
                )
            created += 1
        await session.commit()
    if created:
        logger.info("plan sync: %d plans created", created)
    return {"created": created, "total": len(PLAN_SEED)}
