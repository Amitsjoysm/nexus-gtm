# tests/test_billing_admin_api.py
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def test_admin_billing_requires_platform_admin(client):
    """A normal tenant owner is NOT a platform admin: tenant RBAC must never grant staff access."""
    token = await signup(client, slug="acme", email="owner@acme.com", company="Acme")
    r = await client.get("/api/admin/billing/capabilities", headers=auth(token))
    assert r.status_code == 404


async def test_admin_billing_unauthenticated_is_rejected(client):
    r = await client.get("/api/admin/billing/capabilities")
    assert r.status_code in (401, 404)


async def test_platform_admin_can_read_catalog_and_plans(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()

    # Bootstrap this operator via the env allowlist (no chicken-and-egg DB row needed).
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "staff@nexus.io")
    token = await signup(client, slug="ops", email="staff@nexus.io", company="Ops")
    h = auth(token)

    r = await client.get("/api/admin/billing/capabilities", headers=h)
    assert r.status_code == 200, r.text
    caps = r.json()
    assert len(caps) >= 55
    assert any(c["id"] == "ai.email_draft" for c in caps)

    r = await client.get("/api/admin/billing/capabilities?category=ai", headers=h)
    assert all(c["category"] == "ai" for c in r.json())

    r = await client.get("/api/admin/billing/plans", headers=h)
    assert r.status_code == 200
    plans = r.json()
    growth = next(p for p in plans if p["id"] == "growth")
    assert growth["base_price_cents"] == 7900
    assert growth["entitlement_count"] >= 1
