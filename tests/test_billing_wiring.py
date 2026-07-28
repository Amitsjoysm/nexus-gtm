# tests/test_billing_wiring.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    return await make_tenant()


async def test_metered_records_the_action():
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        async with metered(ts, "ai.email_draft"):
            pass
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 1 and rows[0].capability_id == "ai.email_draft"


async def test_metered_stamps_the_measured_cost():
    """Margin reporting is only real if the cost is measured, not estimated."""
    from nexus.billing.context import report_cost
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        async with metered(ts, "ai.email_draft"):
            report_cost(usd=0.0012, tokens=900, source="groq")
        ev = (await ts.list(BillingUsageEvent))[0]
        assert float(ev.unit_cost_usd) == 0.0012


async def test_metered_refunds_when_the_action_fails():
    """A customer must not be billed for an action that raised. The ledger stays append-only,
    so the correction is a compensating negative row, never a delete."""
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    async with tenant_session(tid) as ts:
        try:
            async with metered(ts, "ai.email_draft"):
                raise RuntimeError("provider exploded")
        except RuntimeError:
            pass
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 2                                  # charge + compensating row
        assert sum(float(r.quantity) for r in rows) == 0       # nets to zero
        assert any(float(r.quantity) < 0 for r in rows)


async def test_metered_is_transparent_when_enforcement_is_off():
    from nexus.billing.meter import metered
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed()
    settings = get_settings()
    original = settings.billing_enforcement
    settings.billing_enforcement = "off"
    try:
        async with tenant_session(tid) as ts:
            async with metered(ts, "ai.email_draft"):
                pass
            assert await ts.list(BillingUsageEvent) == []
    finally:
        settings.billing_enforcement = original


async def test_metered_never_blocks_in_shadow_mode():
    """The whole point of shadow: a tenant far past quota still gets the action."""
    from nexus.billing.meter import metered
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingSubscription

    tid = await _seed()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="free", status="active"))   # quota 20
        await ts.flush()
        for i in range(30):
            await record_usage(ts, capability_id="ai.email_draft", quantity=1,
                               idempotency_key=f"burn{i}")
        async with metered(ts, "ai.email_draft") as m:
            assert m.allowed is True
            assert m.would_block is True        # reported, not enforced
