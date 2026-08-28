# tests/test_capability_burst_defaults.py
"""A per-tenant ceiling on the agent actions a human triggers one at a time.

`burst_limit` has been read by the entitlement engine since M2 and was set on NO plan entitlement
anywhere, so the throttle never fired. Combined with an in-process-only auth rate limit and no
edge limit, one tenant looping an agent endpoint could drive unbounded COGS.

Deliberately narrow. Only capabilities a person triggers ONE AT A TIME are limited: a rep cannot
legitimately draft 240 emails in a minute, but the crawl pipeline genuinely issues hundreds of
`search.web` calls a minute for one tenant, and `verify.email` / `enrich.*` bulk paths record a
single event with `quantity=N` rather than N events — so a burst limit there would either do
nothing or break a legitimate sweep. Getting that wrong turns a safety rail into an outage.

The ceilings are set where no real workflow reaches them; they exist to stop a runaway loop, not
to shape usage. Quotas are what shape usage.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


HUMAN_TRIGGERED = {"ai.email_draft", "ai.call_script", "ai.chat_turn", "ai.research_brief",
                   "ai.account_qa", "ai.icp_from_website"}
# These must NOT be limited — see the docstring.
BULK_OR_PIPELINE = {"search.web", "verify.email", "enrich.account", "enrich.contact",
                    "ai.scoring", "ai.tokens"}


def test_only_human_triggered_capabilities_carry_a_default_burst():
    from nexus.billing.entitlements import DEFAULT_BURST_LIMITS

    assert set(DEFAULT_BURST_LIMITS) == HUMAN_TRIGGERED, (
        "the burst defaults drifted from the set of one-at-a-time actions"
    )
    for cap in BULK_OR_PIPELINE:
        assert cap not in DEFAULT_BURST_LIMITS, (
            f"{cap} runs in bulk or on the crawl pipeline; a burst limit there breaks a real "
            "workflow rather than catching a runaway one"
        )
    for cap, limit in DEFAULT_BURST_LIMITS.items():
        assert limit >= 60, f"{cap} at {limit}/min is tight enough to catch real usage"


async def test_the_default_applies_when_a_plan_says_nothing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import DEFAULT_BURST_LIMITS, resolve_entitlement
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.burst_limit == DEFAULT_BURST_LIMITS["ai.email_draft"]

        # And nothing is invented for a capability outside the map.
        other = await resolve_entitlement(ts, "search.web")
        assert other.burst_limit is None


async def test_a_plan_that_sets_its_own_burst_wins():
    """A negotiated ceiling must outrank the platform default, in both directions."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingPlanEntitlement, BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        # `free` already entitles ai.email_draft (quota 20), and the pair is unique — so this is
        # an edit of the negotiated row, not a second one.
        from sqlalchemy import select

        row = (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == "free",
                    BillingPlanEntitlement.capability_id == "ai.email_draft",
                )
            )
        ).first()
        assert row is not None
        row.burst_limit = 5
        await ts.flush()
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.burst_limit == 5


async def test_an_unlimited_plan_is_not_burst_limited():
    """`unlimited` exists to observe cost, not to gate. A throttle there is still a gate."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="legacy-unlimited", status="active"))
        await ts.flush()
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.mode == "unlimited"
        assert ent.burst_limit is None
