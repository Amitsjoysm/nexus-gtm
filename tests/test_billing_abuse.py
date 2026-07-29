# tests/test_billing_abuse.py
"""Adversarial tests for the metering seam and the money paths.

Each of these is a way a caller could try to get service they have not paid for, rewind a
counter, or drain a balance.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, make_tenant, signup, tenant_session


async def _seed(plan_id: str = "free", slug: str = "t1"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant(slug=slug, name=f"Tenant {slug}")
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


@pytest.fixture
def enforcing(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    return get_settings()


# ---- quantity abuse -------------------------------------------------------------------------

async def test_negative_quantity_cannot_rewind_the_counter(enforcing):
    """Usage is summed, so a negative quantity would REDUCE recorded usage and hand back quota.
    That is free service, so it must never be recorded through the gate."""
    from nexus.billing.entitlements import check_and_meter, current_usage
    from nexus.billing.usage import record_usage

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await record_usage(ts, capability_id="ai.email_draft", quantity=10,
                           idempotency_key="real")
        before = await current_usage(ts, "ai.email_draft")

        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=-100,
                                    idempotency_key="attack")
        assert res.recorded is False
        assert res.reason == "invalid_quantity"
        assert await current_usage(ts, "ai.email_draft") == before


async def test_zero_quantity_is_not_recorded(enforcing):
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=0,
                                    idempotency_key="zero")
        assert res.recorded is False


async def test_absurd_quantity_is_refused(enforcing):
    """A blast-radius cap: one call must not be able to poison a rollup or drain a balance."""
    from nexus.billing.entitlements import check_and_meter, current_usage

    tid = await _seed()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=10**12,
                                    idempotency_key="huge")
        assert res.recorded is False
        assert await current_usage(ts, "ai.email_draft") == 0


async def test_nan_and_infinity_are_refused(enforcing):
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        for bad in (float("nan"), float("inf"), float("-inf")):
            res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=bad,
                                        idempotency_key=f"bad{bad}")
            assert res.recorded is False


async def test_rejecting_a_bad_quantity_still_lets_the_action_through(enforcing):
    """Metering never breaks the product, even when refusing to bill."""
    from nexus.billing.entitlements import check_and_meter

    tid = await _seed()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=-1,
                                    idempotency_key="x")
        assert res.allowed is True


# ---- credit abuse ---------------------------------------------------------------------------

async def test_credits_cannot_be_granted_negative():
    """A negative grant would be a burn that bypasses the balance check."""
    from nexus.billing.credits import balance, grant_credits

    tid = await _seed()
    async with tenant_session(tid) as ts:
        assert await grant_credits(ts, -500, reason="attack", idempotency_key="neg") is False
        assert await balance(ts) == 0


async def test_burn_cannot_overdraw_without_explicit_permission():
    from nexus.billing.credits import balance, burn_credits, grant_credits

    tid = await _seed()
    async with tenant_session(tid) as ts:
        await grant_credits(ts, 10, reason="x", idempotency_key="g")
        assert await burn_credits(ts, 1000, reason="attack", idempotency_key="b") is False
        assert await balance(ts) == 10


async def test_an_idempotency_key_cannot_be_reused_to_double_grant():
    from nexus.billing.credits import balance, grant_credits

    tid = await _seed()
    async with tenant_session(tid) as ts:
        for _ in range(5):
            await grant_credits(ts, 1000, reason="promo", idempotency_key="promo-2026")
        assert await balance(ts) == 1000


async def test_one_tenants_idempotency_key_does_not_block_another():
    """Keys are scoped per tenant; a shared key namespace would let one workspace suppress
    another workspace's billing."""
    from nexus.billing.credits import balance, grant_credits

    a = await _seed(slug="key-a")
    b = await _seed(slug="key-b")
    async with tenant_session(a) as ts:
        await grant_credits(ts, 100, reason="x", idempotency_key="same-key")
    async with tenant_session(b) as ts:
        await grant_credits(ts, 250, reason="x", idempotency_key="same-key")
        assert await balance(ts) == 250
    async with tenant_session(a) as ts:
        assert await balance(ts) == 100


# ---- cross-tenant isolation -----------------------------------------------------------------

async def test_a_tenant_cannot_read_another_tenants_invoices(client):
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    a = auth(await signup(client, slug="abx", email="a@abx.com", company="ABX"))
    b = auth(await signup(client, slug="aby", email="b@aby.com", company="ABY"))
    for headers in (a, b):
        r = await client.get("/api/billing/invoices", headers=headers)
        assert r.status_code == 200
        assert r.json() == []


async def test_a_workspace_owner_cannot_grant_themselves_credits(client):
    """The obvious fraud: mint your own credits."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await signup(client, slug="fraud1", email="o@fraud1.com", company="Fraud1")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "fraud1"))).first()

    r = await client.post(
        f"/api/admin/billing/tenants/{tid}/credits", headers=auth(token),
        json={"amount": 1_000_000, "reason": "free money", "idempotency_key": "k"},
    )
    assert r.status_code in (401, 403)


async def test_a_workspace_owner_cannot_reprice_their_own_plan(client):
    token = await signup(client, slug="fraud2", email="o@fraud2.com", company="Fraud2")
    r = await client.patch(
        "/api/admin/billing/plans/growth", headers=auth(token),
        json={"base_price_cents": 0, "included_credits": 999999},
    )
    assert r.status_code in (401, 403)


async def test_a_workspace_owner_cannot_move_themselves_to_a_better_plan(client):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await signup(client, slug="fraud3", email="o@fraud3.com", company="Fraud3")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "fraud3"))).first()

    r = await client.post(
        f"/api/admin/billing/tenants/{tid}/subscription", headers=auth(token),
        json={"plan_id": "legacy-unlimited"},
    )
    assert r.status_code in (401, 403)


# ---- collection abuse -----------------------------------------------------------------------

async def test_an_invoice_cannot_be_collected_twice(enforcing):
    """Double collection is taking the money twice."""
    from nexus.billing.collection import collect_invoice
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        tid = await _seed("growth")
        async with tenant_session(tid) as ts:
            await rebuild_rollups(ts)
            inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
            await finalize_invoice(ts, inv.id)
            await collect_invoice(ts, inv.id, email="ap@x.test")
            await collect_invoice(ts, inv.id, email="ap@x.test")
            await collect_invoice(ts, inv.id, email="ap@x.test")
        assert len(provider.charges) == 1
    finally:
        set_payment_provider(None)


async def test_a_paid_invoice_cannot_be_re_rated_into_a_smaller_bill(enforcing):
    """Re-rating a closed period must not rewrite a bill the customer already settled."""
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        await rebuild_rollups(ts)
        key = period_key(utcnow(), "period")
        inv = await rate_period(ts, period_key=key)
        await finalize_invoice(ts, inv.id)
        original = inv.total_cents

        again = await rate_period(ts, period_key=key)
        assert again.id == inv.id
        assert again.total_cents == original
        assert again.status == "finalized"
