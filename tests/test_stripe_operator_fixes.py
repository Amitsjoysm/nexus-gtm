"""Four Stripe gaps an operator hit while connecting a live account (reported 2026-09-30).

1. The Payments tab did nothing on its own: `NEXUS_PAYMENT_PROVIDER=stripe` had to be set in the
   deployment environment too, although the tab verifies and activates a credential.
2. Cancelling a subscription in the Superadmin console changed only our row; Stripe kept charging.
3. The customer portal allowed plan switching. We learn the plan only at Checkout, so a switch made
   in the portal charged the new price while the workspace kept the old plan.
4. The webhook panel listed 9 events while the handler handles 13, and the Live/Test badge read
   "Live" for a restricted TEST key (`rk_test_...`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_provider():
    from nexus.billing.payments import set_payment_provider

    set_payment_provider(None)
    yield
    set_payment_provider(None)


# Fake keys keep the real prefix (the mode comes from it) and stay under ten characters after it,
# the length gitleaks' stripe-access-token rule starts at: a longer fake fails the CI secret scan.
async def _active_credential(secret: str = "sk_test_panel1") -> str:
    """A credential as the Payments tab leaves it after Verify and Activate."""
    from nexus.billing.credentials import activate_credential, add_credential
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    row = await add_credential(label="panel", secret_key=secret, webhook_secret="whsec_x")
    async with get_platform_sessionmaker()() as s:
        (await s.get(PaymentCredential, row.id)).status = "verified"
        await s.commit()
    await activate_credential(row.id)
    return row.id


# ---- 1. the Payments tab selects Stripe ---------------------------------------------------------

async def test_an_active_panel_credential_selects_stripe_without_the_environment(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider, resolve_payment_provider

    monkeypatch.setattr(get_settings(), "payment_provider", "noop")
    assert (await resolve_payment_provider()).name == "noop"

    await _active_credential("sk_test_panel1")
    provider = await resolve_payment_provider()
    assert isinstance(provider, StripePaymentProvider)
    assert provider.secret_key == "sk_test_panel1"


async def test_deactivating_the_credential_returns_to_the_environment(monkeypatch):
    from nexus.billing.credentials import deactivate_credential
    from nexus.billing.payments import resolve_payment_provider

    monkeypatch.setattr(get_settings(), "payment_provider", "noop")
    cid = await _active_credential()
    assert (await resolve_payment_provider()).name == "stripe"
    await deactivate_credential(cid)
    assert (await resolve_payment_provider()).name == "noop"


async def test_an_injected_provider_still_wins(monkeypatch):
    from nexus.billing.payments import (
        NoopPaymentProvider,
        resolve_payment_provider,
        set_payment_provider,
    )

    monkeypatch.setattr(get_settings(), "payment_provider", "noop")
    await _active_credential()
    double = NoopPaymentProvider()
    set_payment_provider(double)
    assert await resolve_payment_provider() is double


# ---- 2. cancelling reaches Stripe ---------------------------------------------------------------

class FakeStripe:
    """The provider seam as the admin cancel sees it, recording what it was asked."""

    name = "stripe"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.cancellations: list[dict] = []

    async def cancel_subscription(self, *, subscription_id: str, at_period_end: bool = True):
        if self.error is not None:
            raise self.error
        self.cancellations.append({"id": subscription_id, "at_period_end": at_period_end})
        return {"id": subscription_id, "status": "active" if at_period_end else "canceled",
                "cancel_at_period_end": at_period_end}


async def _superadmin_and_customer(client, monkeypatch, slug: str, *, psp: str = "sub_live123"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription
    from sqlalchemy import select

    email = f"boss@{slug}.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await signup(client, slug=slug, email=email, company=slug.upper())
    await sync_catalog()
    await sync_plans()
    company = f"{slug} Customer"
    await signup(client, slug=f"{slug}-c", email=f"o@{slug}-c.com", company=company)
    rows = (await client.get(f"/api/admin/billing/customers?q={company}",
                             headers=auth(token))).json()
    tid = rows[0]["tenant_id"]
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                          headers=auth(token), json={"plan_id": "growth"})
    assert r.status_code == 200, r.text
    async with get_platform_sessionmaker()() as s:
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        sub.psp_subscription_id = psp
        sub.psp_customer_id = "cus_live123" if psp else None
        await s.commit()
    return token, tid


async def _sub(tid):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription
    from sqlalchemy import select

    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()


async def test_cancelling_at_period_end_cancels_at_stripe_too(client, monkeypatch):
    from nexus.billing.payments import set_payment_provider

    token, tid = await _superadmin_and_customer(client, monkeypatch, "cx1")
    fake = FakeStripe()
    set_payment_provider(fake)
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={"reason": "asked by email"})
    assert r.status_code == 200, r.text
    assert fake.cancellations == [{"id": "sub_live123", "at_period_end": True}]
    assert (await _sub(tid)).cancel_at_period_end is True


async def test_an_immediate_cancellation_ends_it_at_stripe(client, monkeypatch):
    from nexus.billing.payments import set_payment_provider

    token, tid = await _superadmin_and_customer(client, monkeypatch, "cx2")
    fake = FakeStripe()
    set_payment_provider(fake)
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={"at_period_end": False, "reason": "fraud"})
    assert r.status_code == 200, r.text
    assert fake.cancellations == [{"id": "sub_live123", "at_period_end": False}]
    assert (await _sub(tid)).status == "canceled"


async def test_when_stripe_refuses_nothing_changes_here(client, monkeypatch):
    """Marking it cancelled here while Stripe keeps charging is the bug; the reverse is safe."""
    from nexus.billing.payments import PaymentError, set_payment_provider

    token, tid = await _superadmin_and_customer(client, monkeypatch, "cx3")
    set_payment_provider(FakeStripe(PaymentError("stripe /subscriptions -> 404: No such sub")))
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={"at_period_end": False})
    assert r.status_code == 502 and "Nothing was changed" in r.json()["detail"]
    sub = await _sub(tid)
    assert sub.status != "canceled" and not sub.cancel_at_period_end


async def test_a_stripe_subscription_is_not_cancelled_locally_without_stripe(client, monkeypatch):
    """With no Stripe credential in force, a local cancel would stop nothing at Stripe."""
    monkeypatch.setattr(get_settings(), "payment_provider", "noop")
    token, tid = await _superadmin_and_customer(client, monkeypatch, "cx4")
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={})
    assert r.status_code == 409 and "Stripe" in r.json()["detail"]
    assert not (await _sub(tid)).cancel_at_period_end


async def test_an_admin_managed_subscription_cancels_here_only(client, monkeypatch):
    from nexus.billing.payments import set_payment_provider

    token, tid = await _superadmin_and_customer(client, monkeypatch, "cx5", psp="")
    fake = FakeStripe()
    set_payment_provider(fake)
    r = await client.post(f"/api/admin/billing/tenants/{tid}/subscription/cancel",
                          headers=auth(token), json={})
    assert r.status_code == 200 and fake.cancellations == []


async def test_the_stripe_adapter_cancels_the_way_stripe_expects(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, form))
        return {"id": "sub_1", "status": "active", "cancel_at_period_end": True}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    p = StripePaymentProvider("sk_test_x")
    await p.cancel_subscription(subscription_id="sub_1", at_period_end=True)
    await p.cancel_subscription(subscription_id="sub_1", at_period_end=False)
    assert calls[0] == ("POST", "/subscriptions/sub_1", {"cancel_at_period_end": "true"})
    assert calls[1][:2] == ("DELETE", "/subscriptions/sub_1")


# ---- 3. no plan switching in the portal ---------------------------------------------------------

async def test_the_portal_opens_with_plan_switching_off(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import StripePaymentProvider

    payments._portal_configurations.clear()
    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {})))
        if path.startswith("/billing_portal/configurations") and method == "GET":
            return {"data": []}
        if path == "/billing_portal/configurations":
            return {"id": "bpc_nexus"}
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    p = StripePaymentProvider("sk_test_portal")
    await p.create_billing_portal_session(customer_id="cus_1", return_url="https://app/b")
    await p.create_billing_portal_session(customer_id="cus_1", return_url="https://app/b")

    created = [c for c in calls if c[0] == "POST" and c[1] == "/billing_portal/configurations"]
    assert len(created) == 1, "the configuration is created once and reused"
    assert created[0][2]["features[subscription_update][enabled]"] == "false"
    sessions = [c for c in calls if c[1] == "/billing_portal/sessions"]
    assert len(sessions) == 2 and all(c[2]["configuration"] == "bpc_nexus" for c in sessions)


async def test_an_existing_nexus_configuration_is_reused(monkeypatch):
    from nexus.billing import payments
    from nexus.billing.payments import PORTAL_CONFIG_TAG, StripePaymentProvider

    payments._portal_configurations.clear()
    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path))
        if method == "GET":
            return {"data": [
                {"id": "bpc_other", "metadata": {},
                 "features": {"subscription_update": {"enabled": True}}},
                {"id": "bpc_ours", "metadata": {"nexus": PORTAL_CONFIG_TAG},
                 "features": {"subscription_update": {"enabled": False}}},
            ]}
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1",
                "configuration": (form or {}).get("configuration")}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    await StripePaymentProvider("sk_test_reuse").create_billing_portal_session(customer_id="cus_1")
    assert ("POST", "/billing_portal/configurations") not in calls


# ---- 4. the panel tells the truth ---------------------------------------------------------------

def test_every_event_the_handler_branches_on_is_in_the_published_list():
    from nexus.billing.webhooks import HANDLED_EVENTS

    src = (ROOT / "nexus" / "billing" / "webhooks.py").read_text(encoding="utf-8")
    compared = set(re.findall(r'event_type == "([a-z_.]+)"', src))
    assert compared <= set(HANDLED_EVENTS), compared - set(HANDLED_EVENTS)
    assert len(HANDLED_EVENTS) == len(set(HANDLED_EVENTS)) == 13
    for event in ("payment_intent.succeeded", "payment_intent.payment_failed",
                  "charge.refunded", "charge.dispute.created"):
        assert event in HANDLED_EVENTS


async def test_the_webhook_panel_lists_all_thirteen(client, monkeypatch):
    from nexus.billing.webhooks import HANDLED_EVENTS

    email = "boss@hookpanel.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await signup(client, slug="hookpanel", email=email, company="Hook")
    body = (await client.get("/api/admin/runtime/webhook", headers=auth(token))).json()
    assert body["events_handled"] == sorted(HANDLED_EVENTS)


@pytest.mark.parametrize("key, mode", [
    ("sk_test_abc", "test"), ("rk_test_abc", "test"),
    ("sk_live_abc", "live"), ("rk_live_abc", "live"), ("pk_live_abc", "unknown"), ("", "unknown"),
])
def test_the_mode_comes_from_the_key_prefix(key, mode):
    from nexus.billing.credentials import stripe_key_mode

    assert stripe_key_mode(key) == mode


async def test_a_restricted_test_key_is_not_reported_live(monkeypatch):
    from nexus.billing.credentials import add_credential, verify_credential
    from nexus.billing.payments import StripePaymentProvider

    async def account(self, path):
        return {"id": "acct_1", "charges_enabled": True, "email": "ops@example.com"}

    monkeypatch.setattr(StripePaymentProvider, "_get", account)
    test_row = await add_credential(label="restricted test", secret_key="rk_test_restr1")
    live_row = await add_credential(label="restricted live", secret_key="rk_live_restr2")
    assert (await verify_credential(test_row.id))["livemode"] is False
    assert (await verify_credential(live_row.id))["livemode"] is True
