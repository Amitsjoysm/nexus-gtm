# tests/test_billing_checkout_api.py
"""Checkout, against the tiers that are actually on sale.

Retargeted from `growth`/`professional` to `launch`/`accelerate` on 2026-08-26: the ladder was
collapsed to Free / Launch / Accelerate and the old tiers are retired. A retired plan is refused by
checkout with a 409 ("not on sale"), which is correct behaviour and would have made these tests
assert the wrong thing.
The two self-serve money actions: hosted Checkout and the hosted Customer Portal.

Both are redirects. Neither writes a subscription — that is the webhook's job — so the tests
here are about who may open them, which plans are eligible, and what the offline provider was
actually asked for.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup  # noqa: F401  (`client` is an auto-discovered fixture)


@pytest.fixture
def noop_provider():
    """A fresh offline provider for each test, restored afterwards."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    yield provider
    set_payment_provider(None)


async def _seeded_tenant(client, *, slug: str, plan_id: str = "launch"):
    """Signed-up owner with the catalog seeded and a subscription on ``plan_id``."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.security import decode_access_token
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    token = await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.upper())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return token, tid


def _rep_token(tid: str) -> str:
    from nexus.core.security import create_access_token

    return create_access_token(user_id="u-rep", tenant_id=tid, role="rep")


# ---- authz -----------------------------------------------------------------------------------

async def test_checkout_requires_auth(client, noop_provider):
    r = await client.post("/api/billing/checkout", json={"plan_id": "launch"})
    assert r.status_code in (401, 403)


async def test_portal_requires_auth(client, noop_provider):
    r = await client.post("/api/billing/portal", json={})
    assert r.status_code in (401, 403)


async def test_a_rep_cannot_open_checkout(client, noop_provider):
    """Money actions are admin+, unlike the rep-level usage/credits read surface."""
    _, tid = await _seeded_tenant(client, slug="repck")
    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "launch"}, headers=auth(_rep_token(tid))
    )
    assert r.status_code == 403, r.text
    assert noop_provider.checkout_sessions == []


async def test_a_rep_cannot_open_the_portal(client, noop_provider):
    _, tid = await _seeded_tenant(client, slug="reppt")
    r = await client.post(
        "/api/billing/portal", json={}, headers=auth(_rep_token(tid))
    )
    assert r.status_code == 403, r.text
    assert noop_provider.portal_sessions == []


async def test_the_rep_read_surface_is_unchanged(client, noop_provider):
    """Regression guard: tightening the write surface must not lock reps out of usage."""
    _, tid = await _seeded_tenant(client, slug="repread")
    r = await client.get("/api/billing/usage", headers=auth(_rep_token(tid)))
    assert r.status_code == 200, r.text


# ---- checkout --------------------------------------------------------------------------------

async def test_checkout_returns_a_redirect_url(client, noop_provider):
    token, _ = await _seeded_tenant(client, slug="ck1")
    r = await client.post(
        "/api/billing/checkout",
        json={"plan_id": "accelerate", "success_url": "https://app/ok",
              "cancel_url": "https://app/no"},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] and body["id"]
    assert body["plan_id"] == "accelerate"

    sent = noop_provider.checkout_sessions[-1]
    assert sent["plan_id"] == "accelerate"
    assert sent["price_id"]                        # a price object was ensured at the provider
    assert sent["success_url"] == "https://app/ok"


async def test_checkout_stamps_the_tenant_on_the_session(client, noop_provider):
    """The webhook resolves the tenant from this metadata; without it, a completed Checkout
    would have nowhere to land."""
    token, tid = await _seeded_tenant(client, slug="ck2")
    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "launch"}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert noop_provider.checkout_sessions[-1]["metadata"]["tenant_id"] == tid


async def test_checkout_creates_and_stores_the_psp_customer(client, noop_provider):
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    token, tid = await _seeded_tenant(client, slug="ck3")
    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "launch"}, headers=auth(token)
    )
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        assert sub.psp_customer_id                 # reused by the portal and by collection


async def test_checkout_does_not_change_the_subscription(client, noop_provider):
    """State comes back through the webhook. An abandoned Checkout must leave nothing behind."""
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    token, tid = await _seeded_tenant(client, slug="ck4", plan_id="starter")
    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "accelerate"}, headers=auth(token)
    )
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        assert sub.plan_id == "starter"            # unchanged
        assert sub.status == "active"


async def test_checkout_rejects_an_unknown_plan(client, noop_provider):
    token, _ = await _seeded_tenant(client, slug="ck5")
    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "no-such-plan"}, headers=auth(token)
    )
    assert r.status_code == 404, r.text
    assert noop_provider.checkout_sessions == []


async def test_checkout_is_refused_for_an_admin_managed_workspace(client, noop_provider):
    """The enterprise path stays admin-managed: a custom-plan tenant gets a clear 409, never a
    self-serve page that knows nothing about their contract."""
    from nexus.billing.custom_plans import create_custom_plan
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    token, tid = await _seeded_tenant(client, slug="ent1")
    async with get_sessionmaker()() as session:
        await create_custom_plan(
            session, plan_id="custom-ent1", name="Ent1 Deal", base_plan_id="business",
            base_price_cents=250_000, included_credits=100_000,
        )
        await session.commit()
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = "custom-ent1"
        await ts.flush()

    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "launch"}, headers=auth(token)
    )
    assert r.status_code == 409, r.text
    assert "admin-managed" in r.json()["detail"]
    assert noop_provider.checkout_sessions == []


async def test_a_custom_plan_cannot_be_bought_self_serve(client, noop_provider):
    """The other direction: someone else's bespoke deal is not on the public price list."""
    from nexus.billing.custom_plans import create_custom_plan
    from nexus.core.db import get_sessionmaker

    token, _ = await _seeded_tenant(client, slug="ent2")
    async with get_sessionmaker()() as session:
        await create_custom_plan(
            session, plan_id="custom-other", name="Other Deal", base_plan_id="business",
            base_price_cents=1_000, included_credits=10,
        )
        await session.commit()

    r = await client.post(
        "/api/billing/checkout", json={"plan_id": "custom-other"}, headers=auth(token)
    )
    assert r.status_code == 409, r.text
    assert noop_provider.checkout_sessions == []


async def test_unconfigured_stripe_says_so_instead_of_500(client):
    """A deployment that selected Stripe without a key must produce an operator-readable 503,
    not an opaque crash on the customer's upgrade click."""
    from nexus.billing.payments import StripePaymentProvider, set_payment_provider

    set_payment_provider(StripePaymentProvider(""))
    try:
        token, _ = await _seeded_tenant(client, slug="nokey")
        r = await client.post(
            "/api/billing/checkout", json={"plan_id": "launch"}, headers=auth(token)
        )
        assert r.status_code == 503, r.text
        assert "NEXUS_STRIPE_SECRET_KEY" in r.json()["detail"]
    finally:
        set_payment_provider(None)


# ---- portal ----------------------------------------------------------------------------------

async def test_portal_requires_an_existing_payment_account(client, noop_provider):
    token, _ = await _seeded_tenant(client, slug="pt1")
    r = await client.post("/api/billing/portal", json={}, headers=auth(token))
    assert r.status_code == 409, r.text
    assert "no payment account" in r.json()["detail"]


async def test_portal_returns_a_redirect_url_once_a_customer_exists(client, noop_provider):
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    token, tid = await _seeded_tenant(client, slug="pt2")
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.psp_customer_id = "cus_existing"
        await ts.flush()

    r = await client.post(
        "/api/billing/portal", json={"return_url": "https://app/settings"}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"]
    assert noop_provider.portal_sessions[-1]["customer_id"] == "cus_existing"
    assert noop_provider.portal_sessions[-1]["return_url"] == "https://app/settings"


async def test_portal_is_refused_for_an_admin_managed_workspace(client, noop_provider):
    from nexus.billing.custom_plans import create_custom_plan
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    token, tid = await _seeded_tenant(client, slug="ent3")
    async with get_sessionmaker()() as session:
        await create_custom_plan(
            session, plan_id="custom-ent3", name="Ent3 Deal", base_plan_id="business",
            base_price_cents=500_000, included_credits=1,
        )
        await session.commit()
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = "custom-ent3"
        sub.psp_customer_id = "cus_ent3"
        await ts.flush()

    r = await client.post("/api/billing/portal", json={}, headers=auth(token))
    assert r.status_code == 409, r.text
    assert noop_provider.portal_sessions == []
