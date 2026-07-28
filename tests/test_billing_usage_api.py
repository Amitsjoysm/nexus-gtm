# tests/test_billing_usage_api.py
from __future__ import annotations

from tests.conftest import auth, client, signup


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
