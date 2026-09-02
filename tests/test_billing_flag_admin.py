# tests/test_billing_flag_admin.py
"""The write surface for feature flags.

M24 made ``BillingPlanEntitlement.feature_flag`` actually *evaluated*, which fixed half the
problem: an operator could name a flag on an entitlement but had no way to create the flag or turn
it off, so naming one changed nothing (an unknown flag is ON by design). That left the feature in
the same dead-config shape it was built to escape — the switch existed and nothing could move it.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def _admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    await _seed()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_tenant_owner_cannot_touch_flags(client):
    token = await signup(client, slug="ff0", email="o@ff0.com", company="FF0")
    assert (await client.get("/api/admin/billing/flags", headers=auth(token))).status_code in (
        401, 404,
    )
    r = await client.put(
        "/api/admin/billing/flags/beta_x", headers=auth(token), json={"enabled": False},
    )
    assert r.status_code in (401, 404)


async def test_creating_and_listing_a_flag(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ff1", email="boss@ff1.com")

    r = await client.put(
        "/api/admin/billing/flags/beta_network", headers=auth(token),
        json={"enabled": False, "description": "Network beta, off until GA"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    listed = await client.get("/api/admin/billing/flags", headers=auth(token))
    assert listed.status_code == 200
    assert "beta_network" in [f["id"] for f in listed.json()]


async def test_a_flag_write_is_idempotent_and_updates_in_place(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ff2", email="boss@ff2.com")
    await client.put("/api/admin/billing/flags/f2", headers=auth(token), json={"enabled": True})
    await client.put("/api/admin/billing/flags/f2", headers=auth(token), json={"enabled": False})

    listed = await client.get("/api/admin/billing/flags", headers=auth(token))
    rows = [f for f in listed.json() if f["id"] == "f2"]
    assert len(rows) == 1, "a second write must update, not create a duplicate"
    assert rows[0]["enabled"] is False


async def test_a_tenant_override_beats_the_default(client, monkeypatch):
    """The narrowest-first order has to hold through the API, not only in the evaluator."""
    from nexus.billing.flags import flag_enabled
    from tests.conftest import make_tenant, tenant_session

    token = await _admin(client, monkeypatch, slug="ff3", email="boss@ff3.com")
    tid = await make_tenant(slug="ff3t", name="FF3 Target")

    await client.put("/api/admin/billing/flags/f3", headers=auth(token), json={"enabled": False})
    r = await client.put(
        f"/api/admin/billing/flags/f3/overrides/tenant/{tid}",
        headers=auth(token), json={"enabled": True},
    )
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "f3") is True


async def test_clearing_an_override_falls_back_to_the_default(client, monkeypatch):
    """An override must be removable, not only settable — otherwise a beta grant is permanent."""
    from nexus.billing.flags import flag_enabled
    from tests.conftest import make_tenant, tenant_session

    token = await _admin(client, monkeypatch, slug="ff4", email="boss@ff4.com")
    tid = await make_tenant(slug="ff4t", name="FF4 Target")

    await client.put("/api/admin/billing/flags/f4", headers=auth(token), json={"enabled": False})
    await client.put(
        f"/api/admin/billing/flags/f4/overrides/tenant/{tid}",
        headers=auth(token), json={"enabled": True},
    )
    r = await client.delete(
        f"/api/admin/billing/flags/f4/overrides/tenant/{tid}", headers=auth(token),
    )
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        assert await flag_enabled(ts, "f4") is False


async def test_flag_writes_are_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _admin(client, monkeypatch, slug="ff5", email="boss@ff5.com")
    await client.put("/api/admin/billing/flags/f5", headers=auth(token), json={"enabled": False})

    async with get_sessionmaker()() as session:
        actions = [r.action for r in (await session.scalars(select(BillingAuditLog))).all()]
    assert "flag.upsert" in actions


async def test_the_list_reports_which_plans_use_each_flag(client, monkeypatch):
    """A flag with no user is safe to delete; one wired into a paid plan is not. Without this an
    operator has to grep the catalog to find out which."""
    token = await _admin(client, monkeypatch, slug="ff6", email="boss@ff6.com")

    await client.put(
        "/api/admin/billing/plans/growth/entitlements/ai.account_qa",
        headers=auth(token), json={"mode": "metered", "feature_flag": "f6"},
    )
    await client.put("/api/admin/billing/flags/f6", headers=auth(token), json={"enabled": True})

    listed = await client.get("/api/admin/billing/flags", headers=auth(token))
    row = next(f for f in listed.json() if f["id"] == "f6")
    assert "growth" in row["used_by_plans"]
