# tests/test_billing_admin_writes.py
"""The admin write surface: pricing changes without a deploy, but only by a platform admin."""
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def test_admin_writes_reject_a_tenant_owner(client):
    """A workspace owner is not a platform admin. Tenant RBAC must grant nothing here."""
    token = await signup(client, slug="aw1", email="o@aw1.com", company="AW1")
    r = await client.patch("/api/admin/billing/plans/growth",
                           headers=auth(token), json={"base_price_cents": 1})
    assert r.status_code in (401, 403)


async def test_admin_writes_reject_anonymous(client):
    r = await client.patch("/api/admin/billing/plans/growth", json={"base_price_cents": 1})
    assert r.status_code in (401, 403)


async def test_platform_admin_can_reprice_a_plan(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw2", email="boss@nexus.com", company="AW2")

    r = await client.patch("/api/admin/billing/plans/growth", headers=auth(token),
                           json={"base_price_cents": 8900})
    assert r.status_code == 200, r.text
    assert r.json()["base_price_cents"] == 8900


async def test_rate_card_write_refuses_a_below_floor_price(client, monkeypatch):
    """The margin floor is enforced at the API too, not just in the seed — an admin must not be
    able to click past it without recording an exception."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw3", email="boss@nexus.com", company="AW3")

    # 1 credit = $0.01 against $0.012 COGS -> underwater.
    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1})
    assert r.status_code == 422
    assert "margin" in r.text.lower()

    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1, "margin_exception": True,
                               "margin_exception_reason": "strategic loss leader"})
    assert r.status_code == 200, r.text
    assert r.json()["margin_exception"] is True


async def test_credit_grant_is_idempotent(client, monkeypatch):
    """A double-clicked button must not mint credits twice."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw4", email="boss@nexus.com", company="AW4")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "aw4"))).first()

    body = {"amount": 500, "reason": "goodwill", "idempotency_key": "goodwill-2026-07"}
    first = await client.post(f"/api/admin/billing/tenants/{tid}/credits",
                              headers=auth(token), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["applied"] is True
    assert first.json()["balance"] == 500

    second = await client.post(f"/api/admin/billing/tenants/{tid}/credits",
                               headers=auth(token), json=body)
    assert second.status_code == 200, second.text
    assert second.json()["applied"] is False        # same key -> no new credits
    assert second.json()["balance"] == 500


async def test_admin_can_move_a_tenant_between_plans(client, monkeypatch):
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw5", email="boss@nexus.com", company="AW5")

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "aw5"))).first()

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "growth"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == "growth"

    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "professional"})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == "professional"


async def test_unknown_plan_and_capability_are_rejected(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw6", email="boss@nexus.com", company="AW6")

    r = await client.patch("/api/admin/billing/plans/no-such-plan",
                           headers=auth(token), json={"base_price_cents": 1})
    assert r.status_code == 404

    r = await client.put("/api/admin/billing/rates/no.such.capability",
                         headers=auth(token), json={"credits_per_unit": 5})
    assert r.status_code == 404
