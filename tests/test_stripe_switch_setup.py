# tests/test_stripe_switch_setup.py
"""Everything a Stripe account switch needs, managed from the superadmin panel (asked 2026-10-09).

Before this, three things lived outside the app: the app's public address (`NEXUS_APP_BASE_URL`,
which Checkout and the portal return to and the webhook URL is built from), the Stripe Tax switch
(`NEXUS_STRIPE_AUTOMATIC_TAX`), and the webhook endpoint itself, created by hand in the Stripe
dashboard for every account and every mode.
"""
from __future__ import annotations

import pytest

from nexus.billing.payments import StripePaymentProvider
from tests.conftest import auth
from tests.test_admin_permissions import _bootstrap_admin, _make_role
from tests.test_billing_checkout_api import _seeded_tenant

URL = "https://gtm.example.com/api/billing/webhooks/stripe"


@pytest.fixture(autouse=True)
def _restore_runtime_settings(monkeypatch):
    """Same guard as test_runtime_config.py: saving `app_base_url` through the panel mutates the
    cached Settings for the whole worker and marks the key as overridden here, and the next
    refresh would then "restore" it to the environment in the middle of a later test."""
    from nexus.core.config import get_settings
    from nexus.runtime_config import service
    from nexus.runtime_config.catalog import CATALOG

    settings = get_settings()
    for key in CATALOG:
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, getattr(settings, key))
    service._overridden_here.clear()
    yield
    service._overridden_here.clear()


# ---- the two settings are in the panel -----------------------------------------------------------

def test_both_settings_are_runtime_settings():
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import _VALIDATORS

    assert CATALOG["app_base_url"].kind == "str" and "app_base_url" in _VALIDATORS
    assert CATALOG["stripe_automatic_tax"].kind == "bool"
    assert CATALOG["stripe_automatic_tax"].warning          # it breaks checkout when misused


async def test_the_app_address_set_in_the_panel_is_where_checkout_returns(client, monkeypatch):
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        boss = await _bootstrap_admin(client, monkeypatch, "sws1")
        r = await client.put("/api/admin/runtime/settings/app_base_url", headers=auth(boss),
                             json={"value": "https://gtm.example.com"})
        assert r.status_code == 200, r.text
        token, _ = await _seeded_tenant(client, slug="sws1c")
        r = await client.post("/api/billing/checkout", headers=auth(token),
                              json={"plan_id": "accelerate"})
        assert r.status_code == 200, r.text
        assert provider.checkout_sessions[-1]["success_url"].startswith(
            "https://gtm.example.com/settings/billing")
    finally:
        set_payment_provider(None)


async def test_a_bad_app_address_is_refused_before_it_is_stored(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "sws2")
    r = await client.put("/api/admin/runtime/settings/app_base_url", headers=auth(boss),
                         json={"value": "https://gtm.example.com/some/path"})
    assert r.status_code == 400


# ---- the webhook endpoint is created from the app ------------------------------------------------

def _fake(monkeypatch, responder):
    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {})))
        return responder(method, path, dict(form or {}))

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    return calls


async def test_the_endpoint_is_created_pinned_and_only_our_old_one_replaced(monkeypatch):
    from nexus.billing.payments import WEBHOOK_TAG
    from nexus.billing.webhooks import HANDLED_EVENTS

    def responder(m, p, f):
        if m == "GET":
            return {"data": [
                {"id": "we_old", "url": URL, "metadata": {"nexus": WEBHOOK_TAG}},
                {"id": "we_other", "url": URL, "metadata": {}},
                {"id": "we_elsewhere", "url": "https://other.example.com/hook",
                 "metadata": {"nexus": WEBHOOK_TAG}},
            ]}
        if m == "POST":
            return {"id": "we_new", "url": URL, "secret": "whsec_new"}
        return {"id": p.rsplit("/", 1)[1], "deleted": True}

    calls = _fake(monkeypatch, responder)
    out = await StripePaymentProvider("sk_test_x").create_webhook_endpoint(
        url=URL, events=HANDLED_EVENTS)
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["url"] == URL
    assert post[2]["api_version"] == StripePaymentProvider.API_VERSION
    assert sorted(v for k, v in post[2].items() if k.startswith("enabled_events")) == sorted(
        HANDLED_EVENTS)
    assert post[2]["metadata[nexus]"] == WEBHOOK_TAG
    assert [c[1] for c in calls if c[0] == "DELETE"] == ["/webhook_endpoints/we_old"]
    assert out == {"id": "we_new", "url": URL, "secret": "whsec_new",
                   "replaced": ["we_old"], "others": ["we_other"]}


async def _credential() -> str:
    from nexus.billing.credentials import add_credential
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    row = await add_credential(label="live", secret_key="sk_test_hook1")
    async with get_platform_sessionmaker()() as s:
        (await s.get(PaymentCredential, row.id)).status = "verified"
        await s.commit()
    return row.id


async def _webhook_secret(cid: str) -> str:
    from nexus.core.crypto import unseal_text
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    async with get_platform_sessionmaker()() as s:
        row = await s.get(PaymentCredential, cid)
        return unseal_text(row.webhook_secret_encrypted) if row.webhook_secret_encrypted else ""


async def test_a_superadmin_creates_it_and_the_signing_secret_is_saved(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_base_url", "https://gtm.example.com")
    _fake(monkeypatch, lambda m, p, f: (
        {"data": []} if m == "GET" else {"id": "we_1", "url": f.get("url"), "secret": "whsec_1"}))
    boss = await _bootstrap_admin(client, monkeypatch, "sws3")
    cid = await _credential()
    r = await client.post(f"/api/admin/payment-credentials/{cid}/webhook-endpoint",
                          headers=auth(boss))
    assert r.status_code == 200, r.text
    assert r.json()["url"] == URL and len(r.json()["events"]) == 13
    assert "secret" not in r.json()                      # it is stored, never returned
    assert await _webhook_secret(cid) == "whsec_1"


async def test_it_needs_a_public_address_first(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_base_url", "")
    boss = await _bootstrap_admin(client, monkeypatch, "sws4")
    cid = await _credential()
    r = await client.post(f"/api/admin/payment-credentials/{cid}/webhook-endpoint",
                          headers=auth(boss))
    assert r.status_code == 409 and "public address" in r.text


@pytest.mark.parametrize("role", ["finance", "support"])
async def test_only_a_superadmin_can_create_it(client, monkeypatch, role):
    boss = await _bootstrap_admin(client, monkeypatch, f"sws5{role[0]}")
    other = await _make_role(client, boss, f"x@{role}.com", role, f"sws5{role[0]}x")
    cid = await _credential()
    r = await client.post(f"/api/admin/payment-credentials/{cid}/webhook-endpoint",
                          headers=auth(other))
    assert r.status_code == 403
