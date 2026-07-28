# tests/test_billing_usage_api.py
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def test_usage_endpoint_requires_auth(client):
    r = await client.get("/api/billing/usage")
    assert r.status_code in (401, 403)


async def test_usage_endpoint_returns_capabilities_with_quota(client):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="use", email="u@use.com", company="Use")
    r = await client.get("/api/billing/usage", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "plan" in body and "period" in body and isinstance(body["capabilities"], list)


async def test_usage_never_leaks_another_tenant(client):
    """Tenant isolation on the billing surface."""
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    t1 = auth(await signup(client, slug="ba", email="a@ba.com", company="BA"))
    t2 = auth(await signup(client, slug="bb", email="b@bb.com", company="BB"))
    r1 = await client.get("/api/billing/usage", headers=t1)
    r2 = await client.get("/api/billing/usage", headers=t2)
    assert r1.status_code == 200 and r2.status_code == 200
    # Each tenant sees its own (empty) usage, never the other's rows.
    for body in (r1.json(), r2.json()):
        assert all(c["used"] == 0 for c in body["capabilities"])


async def test_usage_endpoint_agrees_with_the_enforced_counter(client):
    """The number shown to a rep must equal the number enforcement uses.

    Enforcement counts the period rollup plus the events not yet rolled up. If this endpoint
    read only the rollup, a rep would see "17 of 20" while a 402 told them they were at 20.
    """
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import current_usage
    from nexus.billing.plans import sync_plans
    from nexus.billing.rollups import rebuild_rollups
    from nexus.billing.usage import record_usage
    from nexus.core.tenancy import TenantSession
    from nexus.models.identity import Tenant
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="agree", email="a@agree.com", company="Agree")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "agree"))).first()

    async with get_sessionmaker()() as s:
        ts = TenantSession(s, tid)
        await record_usage(ts, capability_id="verify.email", quantity=4, idempotency_key="a")
        await rebuild_rollups(ts)                                    # 4 is rolled up
        await record_usage(ts, capability_id="verify.email", quantity=3, idempotency_key="b")
        await s.commit()                                             # 3 is NOT rolled up
        assert await current_usage(ts, "verify.email") == 7

    r = await client.get("/api/billing/usage", headers=auth(token))
    assert r.status_code == 200, r.text
    row = next(c for c in r.json()["capabilities"] if c["capability_id"] == "verify.email")
    assert row["used"] == 7
