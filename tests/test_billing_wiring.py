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


async def test_quota_exceeded_renders_a_useful_402():
    """A dead 500 teaches the customer nothing; a 402 with the upgrade path converts.

    Builds its own app rather than using the `client` fixture, because the probe route has to be
    registered before the SPA catch-all mount that `create_app` adds last.
    """
    import httpx
    from fastapi import APIRouter

    from nexus.billing.errors import QuotaExceeded
    from nexus.main import create_app

    app = create_app()

    r = APIRouter()

    @r.get("/__quota_probe")
    async def probe():
        raise QuotaExceeded("ai.email_draft", reason="quota_exhausted", used=20, quota=20)

    app.include_router(r)
    # The SPA is mounted at "/" and would otherwise swallow the probe.
    app.router.routes.insert(0, app.router.routes.pop())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/__quota_probe")

    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["error"] == "quota_exceeded"
    assert body["capability"] == "ai.email_draft"
    assert body["upgrade_url"]


async def test_running_an_agent_records_usage(client):
    """The highest-leverage seam: one wrap on /agents/{name}/run covers every AI action."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.billing import BillingUsageEvent
    from nexus.models.identity import Tenant
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="wire", email="w@wire.com", company="Wire")

    r = await client.post("/api/agents/research/run", headers=auth(token), json={"inputs": {}})
    assert r.status_code == 200, r.text

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "wire"))).first()
        ts = TenantSession(s, tid)
        rows = await ts.list(BillingUsageEvent)

    assert len(rows) == 1
    assert rows[0].capability_id == "ai.research_brief"
    assert rows[0].attrs.get("agent") == "research"


async def test_an_unknown_agent_is_never_billed(client):
    """404 must not cost the customer anything."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.billing import BillingUsageEvent
    from nexus.models.identity import Tenant
    from tests.conftest import auth, signup

    await sync_catalog()
    token = await signup(client, slug="unk", email="u@unk.com", company="Unk")

    r = await client.post("/api/agents/nope/run", headers=auth(token), json={"inputs": {}})
    assert r.status_code == 404

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "unk"))).first()
        ts = TenantSession(s, tid)
        assert await ts.list(BillingUsageEvent) == []


async def test_reverify_meters_one_unit_per_email():
    """Quantity must reflect real consumption: 12 verifications is 12 units, not one call."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.meter import metered
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        async with metered(ts, "verify.email", quantity=12):
            pass
        ev = (await ts.list(BillingUsageEvent))[0]
        assert float(ev.quantity) == 12
