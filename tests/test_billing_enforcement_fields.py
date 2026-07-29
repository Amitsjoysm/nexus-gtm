# tests/test_billing_enforcement_fields.py
"""Configuration that used to be stored and never evaluated.

`depends_on`, `burst_limit` and `BillingThrottled` all existed as data and dead code: a plan
could say "requires module.network, 60/min" and neither clause did anything. These pin the
behaviour now that both are read.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


async def _catalog():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def _set_entitlement_mode(ts, plan_id: str, capability_id: str, mode: str):
    """Upsert. The seed already gives most plans a row for the module capabilities, so a blind
    insert trips the (plan_id, capability_id) unique constraint."""
    from sqlalchemy import select

    from nexus.models.billing import BillingPlanEntitlement

    row = (
        await ts.session.scalars(
            select(BillingPlanEntitlement).where(
                BillingPlanEntitlement.plan_id == plan_id,
                BillingPlanEntitlement.capability_id == capability_id,
            )
        )
    ).first()
    if row is None:
        row = BillingPlanEntitlement(plan_id=plan_id, capability_id=capability_id)
        ts.session.add(row)
    row.mode = mode
    await ts.flush()
    return row


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


# ---- dependency gating ---------------------------------------------------------------------

async def test_capability_is_disabled_when_its_module_is(enforcing):
    """A module gate must actually gate. Network search depends on module.network."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.models.billing import BillingCapability, BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()

        cap = await ts.session.get(BillingCapability, "network.search")
        cap.depends_on = ["module.network"]
        await ts.flush()
        # Free does not include the Network module.
        await _set_entitlement_mode(ts, "free", "module.network", "disabled")

        res = await resolve_entitlement(ts, "network.search")
        assert res.mode == "disabled"
        assert res.source == "dependency"


async def test_capability_is_unaffected_when_its_module_is_enabled(enforcing):
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.models.billing import BillingCapability, BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        cap = await ts.session.get(BillingCapability, "network.search")
        cap.depends_on = ["module.network"]
        await ts.flush()
        await _set_entitlement_mode(ts, "free", "module.network", "enabled")

        assert (await resolve_entitlement(ts, "network.search")).mode != "disabled"


async def test_unknown_dependency_does_not_block(enforcing):
    """Unknown always means allow — cataloguing a capability late must not break a live feature."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.models.billing import BillingCapability, BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        cap = await ts.session.get(BillingCapability, "network.search")
        cap.depends_on = ["module.does_not_exist"]
        await ts.flush()

        assert (await resolve_entitlement(ts, "network.search")).mode != "disabled"


async def test_self_referencing_dependency_terminates(enforcing):
    """A bad catalog edit must not hang the metering hot path."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.models.billing import BillingCapability, BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        cap = await ts.session.get(BillingCapability, "network.search")
        cap.depends_on = ["network.search"]
        await ts.flush()

        res = await resolve_entitlement(ts, "network.search")
        assert res.capability_id == "network.search"     # returned, did not recurse forever


# ---- burst limits --------------------------------------------------------------------------

async def _set_burst(ts, plan_id: str, capability_id: str, limit: int):
    from nexus.models.billing import BillingPlanEntitlement

    ts.session.add(
        BillingPlanEntitlement(plan_id=plan_id, capability_id=capability_id,
                               mode="metered", quota=None, burst_limit=limit)
    )
    await ts.flush()


async def test_burst_limit_throttles_when_enforcing(enforcing):
    from nexus.billing.entitlements import check_and_meter
    from nexus.models.billing import BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        await _set_burst(ts, "free", "ai.chat_turn", 3)

        for i in range(3):
            r = await check_and_meter(ts, capability_id="ai.chat_turn",
                                      idempotency_key=f"b{i}")
            assert r.allowed is True

        blocked = await check_and_meter(ts, capability_id="ai.chat_turn",
                                        idempotency_key="b3")
        assert blocked.allowed is False
        assert blocked.reason == "throttled"


async def test_burst_limit_reports_but_never_blocks_in_shadow():
    from nexus.billing.entitlements import check_and_meter
    from nexus.models.billing import BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        await _set_burst(ts, "free", "ai.chat_turn", 2)

        for i in range(4):
            r = await check_and_meter(ts, capability_id="ai.chat_turn",
                                      idempotency_key=f"s{i}")
            assert r.allowed is True
        assert r.would_block is True


async def test_no_burst_limit_means_no_throttle(enforcing):
    """Capabilities without a limit must not pay for a check they never asked for."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.models.billing import BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()
        for i in range(12):
            r = await check_and_meter(ts, capability_id="ai.chat_turn",
                                      idempotency_key=f"n{i}")
        assert r.allowed is True


async def test_throttle_raises_429_not_402(enforcing):
    """A rate limit is not an upsell: telling someone to upgrade when they need to slow down
    is the wrong instruction."""
    from nexus.billing.entitlements import MeterResult, ResolvedEntitlement
    from nexus.billing.errors import BillingThrottled

    ent = ResolvedEntitlement("ai.chat_turn", mode="metered")
    result = MeterResult(allowed=False, reason="throttled", entitlement=ent)
    with pytest.raises(BillingThrottled) as exc:
        result.raise_if_blocked()
    assert exc.value.retry_after_s > 0


async def test_an_explicit_plan_entitlement_outranks_the_module_gate(enforcing):
    """The seed's own contradiction: Free disables module.outreach but still sells 20
    ai.email_drafts. A plan that explicitly prices and quotas a capability means it, so the
    module gate must not silently revoke what the plan sold."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.models.billing import BillingSubscription

    await _catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))
        await ts.flush()

        res = await resolve_entitlement(ts, "ai.email_draft")
        assert res.mode == "metered"
        assert res.quota == 20
        assert res.source == "plan"

        # ...while a capability the plan does NOT mention still obeys the gate.
        gated = await resolve_entitlement(ts, "network.search")
        assert gated.mode == "disabled"
        assert gated.source == "dependency"
