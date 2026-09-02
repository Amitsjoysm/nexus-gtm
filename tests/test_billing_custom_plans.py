# tests/test_billing_custom_plans.py
"""Per-customer negotiated pricing, built in Admin and published to the payment provider.

A bespoke deal becomes a real plan row, so the entitlement engine, rating, and the admin UI all
treat it exactly like a standard plan. Enterprise pricing adds no new code paths.
"""
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def _admin(client, monkeypatch, slug: str):
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())

    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == slug))).first()
    return token, tid


async def test_admin_builds_a_custom_plan_and_assigns_it(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        token, tid = await _admin(client, monkeypatch, "cp1")
        r = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={
                "base_plan_id": "growth",
                "name": "Acme Enterprise Deal",
                "base_price_cents": 250000,
                "included_credits": 100000,
                "entitlement_overrides": {
                    "verify.email": {"quota": 250000, "overage_price_credits": 0},
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["plan_id"] == "custom-cp1"
        assert body["base_price_cents"] == 250000
        assert body["entitlements_cloned"] > 0
        assert body["overrides_applied"] == 1
        assert body["assigned"] is True
        # Published to the provider as a real product + price.
        assert body["provider"]["price_id"]
        assert provider.prices["custom-cp1"]["amount_cents"] == 250000
    finally:
        set_payment_provider(None)


async def test_the_custom_plan_actually_drives_entitlements(client, monkeypatch):
    """The point of using a plan row: the engine resolves it with no special-casing."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.workers.tasks import tenant_session

    set_payment_provider(NoopPaymentProvider())
    try:
        token, tid = await _admin(client, monkeypatch, "cp2")
        await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={
                "base_plan_id": "growth", "base_price_cents": 500000,
                "included_credits": 50000,
                "entitlement_overrides": {"verify.email": {"quota": 999000}},
            },
        )
        async with tenant_session(tid) as ts:
            ent = await resolve_entitlement(ts, "verify.email")
            assert ent.plan_id == "custom-cp2"
            assert ent.quota == 999000          # the negotiated number, not Growth's 5000
    finally:
        set_payment_provider(None)


async def test_custom_plan_is_idempotent_and_updatable(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    set_payment_provider(NoopPaymentProvider())
    try:
        token, tid = await _admin(client, monkeypatch, "cp3")
        first = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": 100000},
        )
        second = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": 120000},
        )
        assert first.json()["created"] is True
        assert second.json()["created"] is False        # same plan, repriced
        assert second.json()["base_price_cents"] == 120000
    finally:
        set_payment_provider(None)


async def test_custom_plan_rejects_a_tenant_owner(client):
    """Bespoke pricing is a platform-admin power. A workspace owner must not price themselves."""
    token = await signup(client, slug="cp4", email="o@cp4.com", company="CP4")
    r = await client.post(
        "/api/admin/billing/tenants/whatever/custom-plan", headers=auth(token),
        json={"base_plan_id": "growth", "base_price_cents": 0},
    )
    assert r.status_code in (401, 404)


async def test_custom_plan_rejects_bad_input(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    set_payment_provider(NoopPaymentProvider())
    try:
        token, tid = await _admin(client, monkeypatch, "cp5")

        unknown_base = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "no-such-plan", "base_price_cents": 100},
        )
        assert unknown_base.status_code == 422

        negative = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": -1},
        )
        assert negative.status_code == 422

        unknown_tenant = await client.post(
            "/api/admin/billing/tenants/does-not-exist/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": 100},
        )
        assert unknown_tenant.status_code == 404
    finally:
        set_payment_provider(None)


async def test_custom_plan_creation_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    set_payment_provider(NoopPaymentProvider())
    try:
        token, tid = await _admin(client, monkeypatch, "cp6")
        await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": 777000},
        )
        async with get_sessionmaker()() as s:
            rows = list((await s.scalars(
                select(BillingAuditLog).where(
                    BillingAuditLog.action == "custom_plan.create"
                )
            )).all())
        assert len(rows) == 1
        assert rows[0].subject_tenant_id == tid
        assert rows[0].after["base_price_cents"] == 777000
    finally:
        set_payment_provider(None)


async def test_provider_failure_does_not_lose_the_deal(client, monkeypatch):
    """A PSP outage must not leave a half-built plan; publishing can be retried."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    class Broken(NoopPaymentProvider):
        async def ensure_plan_price(self, **kwargs):
            raise RuntimeError("stripe down")

    set_payment_provider(Broken())
    try:
        token, tid = await _admin(client, monkeypatch, "cp7")
        r = await client.post(
            f"/api/admin/billing/tenants/{tid}/custom-plan", headers=auth(token),
            json={"base_plan_id": "growth", "base_price_cents": 90000},
        )
        assert r.status_code == 200
        assert "error" in r.json()["provider"]
        assert r.json()["base_price_cents"] == 90000     # the plan still exists locally
    finally:
        set_payment_provider(None)
