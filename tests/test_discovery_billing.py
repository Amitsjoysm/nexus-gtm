# tests/test_discovery_billing.py
"""A discovery sweep bills for the accounts it delivers, not the candidates it considered.

Measured on the live deployment 2026-09-03, on a workspace with a 200-credit free plan:

    enrich.account  quantity=40  burned 120 credits  attrs={"batch": true}  user_id=NULL
    accounts actually added: 20

The price was right (40 x 3 = 120, exactly the rate card) and the capability was wrong. Candidate
enrichment is speculative work the customer never receives — most of those 40 fail the ICP gate
and are discarded — while `discovery.account_added` exists at 5 credits with the COGS line
"exa pool + **enrich amortized**", which is precisely this: the cost of enriching the candidates
that did not make it, amortised across the ones that did.

So the same business event was priced two ways depending on which code path billed it, and the
wrong one consumed 60% of a free tenant's monthly balance in a single unrequested sweep.

Billed AFTER the sweep, because the number of accounts that clear the ICP gate is not knowable
until they have been enriched and scored — the same shape as the bulk verifier in
`routers/contacts.py`, where enforcement applies to the next run rather than guessing at this one.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def _paid_tenant(credits: float = 1000):
    """A tenant on a real plan. An `unlimited` class never burns, so a test against one would
    pass whatever the call site did."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="launch", status="active"))
        await ts.flush()
        await grant_credits(ts, credits, reason="t", idempotency_key="g")
    return tid


async def _usage(ts, capability_id: str) -> float:
    from sqlalchemy import func, select

    from nexus.models.billing import BillingUsageEvent

    total = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
        )
    )
    return float(total or 0)


async def test_candidate_enrichment_inside_a_sweep_is_not_billed_as_enrichment():
    """The 40 candidates are ours to consider, not the customer's to pay for."""
    from nexus.discovery.auto import _enrich_candidates
    from nexus.models.account import Account

    tid = await _paid_tenant()
    async with tenant_session(tid) as ts:
        built = [
            Account(tenant_id=tid, name=f"C{i}", domain=f"c{i}.com") for i in range(40)
        ]
        await _enrich_candidates(ts, built, concurrency=2)
        charged = await _usage(ts, "enrich.account")

    assert charged == 0, (
        f"a discovery sweep billed {charged} units of enrich.account for candidates the customer "
        "never received; discovery.account_added is the line that covers this"
    )


async def test_a_direct_enrich_batch_still_bills_as_before():
    """The change must not reach the other caller. `enrich_batch` keeps billing by default."""
    from nexus.enrichment.account import get_account_enricher
    from nexus.models.account import Account

    tid = await _paid_tenant()
    async with tenant_session(tid) as ts:
        accounts = [Account(tenant_id=tid, name=f"D{i}", domain=f"d{i}.com") for i in range(3)]
        await get_account_enricher().enrich_batch(ts, accounts, concurrency=2)
        assert await _usage(ts, "enrich.account") == 3


async def test_every_account_the_sweep_persists_is_billed_once():
    from nexus.discovery.auto import _meter_discovered

    tid = await _paid_tenant()
    async with tenant_session(tid) as ts:
        await _meter_discovered(ts, 20)
        assert await _usage(ts, "discovery.account_added") == 20


async def test_a_sweep_that_added_nothing_is_not_billed():
    """Screening 40 candidates and keeping none is a sweep that sold nothing."""
    from nexus.discovery.auto import _meter_discovered

    tid = await _paid_tenant()
    async with tenant_session(tid) as ts:
        await _meter_discovered(ts, 0)
        assert await _usage(ts, "discovery.account_added") == 0


async def test_the_charge_is_the_rate_card_price():
    """discovery.account_added is 5 credits; 20 accounts is 100, not the 120 it used to cost."""
    from nexus.billing.credits import balance
    from nexus.discovery.auto import _meter_discovered

    tid = await _paid_tenant()
    async with tenant_session(tid) as ts:
        before = await balance(ts)
        await _meter_discovered(ts, 20)
        spent = before - await balance(ts)
    assert spent == 100, f"expected 20 x 5 credits, charged {spent}"


async def test_billing_never_breaks_the_sweep():
    """A background job must not die because metering did. The accounts are already persisted."""
    import nexus.billing.meter as meter_mod
    from nexus.discovery.auto import _meter_discovered

    tid = await _paid_tenant()
    original = meter_mod.metered

    def boom(*a, **kw):
        raise RuntimeError("billing is down")

    meter_mod.metered = boom
    try:
        async with tenant_session(tid) as ts:
            await _meter_discovered(ts, 5)   # must not raise
    finally:
        meter_mod.metered = original


async def test_a_plan_without_discovery_does_not_get_a_free_sweep():
    """Routing the charge to the right capability exposed a tenant getting the feature free.

    `free` disables `module.discovery`, and `discovery.account_added` depends on it — so the
    charge is refused. Before this change the sweep billed `enrich.account`, which free DOES
    include, so the work was paid for through the wrong door and nobody noticed the entitlement
    was being ignored. Measured live: a `free` workspace ran a sweep, took 5 accounts, and was
    charged nothing.

    The answer is not to bill it anyway — the plan says they do not have this. It is to not do
    the work. Checked BEFORE the search, so an excluded tenant costs us nothing rather than
    costing us a full sweep we then fail to invoice.
    """
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.discovery.auto import auto_discover_for_tenant
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingSubscription
    from nexus.relevance.engine import get_or_create_profile

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        profile = await get_or_create_profile(ts)
        profile.icp = {"industries": ["SaaS"], "countries": ["United States"],
                       "employee_min": 10, "employee_max": 5000}
        await ts.flush()

    settings = get_settings()
    previous = settings.billing_enforcement
    settings.billing_enforcement = "on"
    try:
        async with tenant_session(tid) as ts:
            res = await auto_discover_for_tenant(
                ts, target_count=5, min_fit=60, pool_limit=10
            )
    finally:
        settings.billing_enforcement = previous

    assert res.get("skipped") == "not_entitled", (
        f"a plan without module.discovery still ran a sweep: {res}"
    )
    assert res.get("discovered", 0) == 0
