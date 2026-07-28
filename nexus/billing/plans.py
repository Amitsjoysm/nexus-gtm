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
        "id": "free", "name": "Free", "plan_class": "free", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 100,
        "max_seats": 1, "sort_order": 10,
        "description": "Explore NEXUS with a single seat.",
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
        "id": "starter", "name": "Starter", "plan_class": "standard", "status": "active",
        "base_price_cents": 3900, "seat_price_cents": 3900, "included_credits": 750,
        "max_seats": 5, "sort_order": 20,
        "description": "For a first SDR running outbound.",
        "entitlements": _STARTER_ENT,
    },
    {
        "id": "growth", "name": "Growth", "plan_class": "standard", "status": "active",
        "base_price_cents": 7900, "seat_price_cents": 7900, "included_credits": 2000,
        "max_seats": 25, "sort_order": 30,
        "description": "Full GTM stack for a growing team.",
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "professional", "name": "Professional", "plan_class": "standard",
        "status": "active", "base_price_cents": 12900, "seat_price_cents": 12900,
        "included_credits": 4000, "max_seats": 100, "sort_order": 40,
        "description": "API access and priority support.",
        "entitlements": _PRO_ENT,
    },
    {
        "id": "business", "name": "Business", "plan_class": "standard", "status": "active",
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
