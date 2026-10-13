"""Choosing which Stripe payment method configuration Checkout uses (2026-10-13).

Asked for after staging's Checkout showed "There are no valid payment methods available": the
operator made a new configuration in Stripe and found the app had no way to use it. Checkout named
none, so Stripe always used the account's Default Config.

- Optional, per Stripe credential (a configuration belongs to one Stripe account; migration 0070).
  Empty is the Default Config, exactly as before.
- Read back from Stripe with that credential's own key before it is stored: an id from another
  account, an inactive configuration or one with cards off is refused, with the reason.
- The active credential's choice is sent with every Checkout, and Check checkout and Platform
  health read that configuration rather than the default.
"""
from __future__ import annotations

import pytest

from nexus.billing.payments import PaymentError, StripePaymentProvider
from tests.conftest import auth

GOOD = "pmc_1GoodConfig0001"
ACCOUNT = {"id": "acct_1", "country": "US", "default_currency": "usd", "charges_enabled": True,
           "capabilities": {"card_payments": "active"}, "requirements": {"currently_due": []}}


def _config(*, active=True, card="on", available=True) -> dict:
    return {"id": GOOD, "active": active, "is_default": False,
            "card": {"available": available, "display_preference": {"value": card}}}


def _stripe(monkeypatch, configs: dict[str, dict]):
    """Answer Stripe's API from `configs`: a missing id is the 404 another account gives."""
    seen: list[tuple[str, str, dict]] = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        seen.append((method, path, dict(form or {})))
        if path.startswith("/account"):
            return ACCOUNT
        if path.startswith("/payment_method_configurations/"):
            pmc = path.rsplit("/", 1)[1]
            if pmc not in configs:
                err = PaymentError(f"stripe {path} -> 404: No such payment method configuration")
                err.status = 404
                raise err
            return configs[pmc]
        if path.startswith("/payment_method_configurations"):
            return {"data": [{"id": "pmc_default", "is_default": True, "active": True,
                              "card": {"available": True, "display_preference": {"value": "on"}}}]}
        return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    return seen


async def _put(client, token, cid, value):
    return await client.put(f"/api/admin/payment-credentials/{cid}/payment-method-configuration",
                            headers=auth(token), json={"configuration_id": value})


@pytest.fixture
async def admin(client, monkeypatch):
    from tests.test_admin_permissions import _bootstrap_admin
    from tests.test_stripe_switch_setup import _credential

    token = await _bootstrap_admin(client, monkeypatch, "pmcadmin")
    return token, await _credential()


async def test_a_configuration_from_this_account_is_stored_and_cleared(client, monkeypatch,
                                                                       admin):
    token, cid = admin
    _stripe(monkeypatch, {GOOD: _config()})
    r = await _put(client, token, cid, GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["payment_method_configuration"] == GOOD
    listed = (await client.get("/api/admin/payment-credentials", headers=auth(token))).json()
    assert next(c for c in listed if c["id"] == cid)["payment_method_configuration"] == GOOD
    r = await _put(client, token, cid, "")
    assert r.status_code == 200 and r.json()["payment_method_configuration"] == ""


@pytest.mark.parametrize(("configs", "value", "words"), [
    ({}, GOOD, "is not a payment method configuration in"),        # another account's id
    ({GOOD: _config(active=False)}, GOOD, "switched off"),
    ({GOOD: _config(card="off")}, GOOD, "Cards are off"),
    ({GOOD: _config(available=False)}, GOOD, "Cards are off"),
    ({}, "not-a-pmc", "looks like pmc_"),
])
async def test_what_would_break_checkout_is_refused(client, monkeypatch, admin, configs, value,
                                                    words):
    token, cid = admin
    _stripe(monkeypatch, configs)
    r = await _put(client, token, cid, value)
    assert r.status_code == 400, r.text
    assert words in r.json()["detail"]


async def test_the_active_credentials_choice_reaches_checkout(client, monkeypatch, admin):
    from nexus.billing.payments import resolve_payment_provider, set_payment_provider
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    token, cid = admin
    seen = _stripe(monkeypatch, {GOOD: _config()})
    assert (await _put(client, token, cid, GOOD)).status_code == 200
    async with get_platform_sessionmaker()() as s:
        (await s.get(PaymentCredential, cid)).active = True
        await s.commit()
    set_payment_provider(None)
    try:
        provider = await resolve_payment_provider()
        assert provider.payment_method_configuration == GOOD
        await provider.create_checkout_session(tenant_id="t1", plan_id="launch",
                                               price_id="price_1", success_url="https://x/s",
                                               cancel_url="https://x/c")
        form = next(f for m, p, f in seen if p == "/checkout/sessions")
        assert form["payment_method_configuration"] == GOOD
    finally:
        set_payment_provider(None)


async def test_without_a_choice_checkout_names_none(monkeypatch):
    seen = _stripe(monkeypatch, {})
    await StripePaymentProvider("sk_test_none1").create_checkout_session(
        tenant_id="t1", plan_id="launch", price_id="price_1", success_url="https://x/s",
        cancel_url="https://x/c")
    form = next(f for m, p, f in seen if p == "/checkout/sessions")
    assert "payment_method_configuration" not in form


@pytest.mark.parametrize(("configs", "blocking"), [
    ({GOOD: _config()}, None),
    ({}, "is not in this Stripe account"),
    ({GOOD: _config(active=False)}, "switched off"),
    ({GOOD: _config(card="off")}, "Cards are switched off"),
])
async def test_check_checkout_reads_the_chosen_configuration(monkeypatch, configs, blocking):
    from nexus.billing import checkout_readiness

    checkout_readiness.clear_cache()
    seen = _stripe(monkeypatch, configs)
    provider = StripePaymentProvider("sk_live_pmccheck", payment_method_configuration=GOOD)
    r = await checkout_readiness.checkout_readiness(provider, currencies={"USD"})
    assert any(p == f"/payment_method_configurations/{GOOD}" for _m, p, _f in seen)
    if blocking is None:
        assert r.ok, r.blocking
    else:
        assert not r.ok and any(blocking in b for b in r.blocking), r.blocking


def test_the_payments_tab_offers_the_field():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "frontend" / "src"
    tab = (root / "pages" / "admin" / "PaymentsTab.tsx").read_text(encoding="utf-8")
    api = (root / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "setPaymentMethodConfiguration" in tab and "Payment method configuration" in tab
    assert "/payment-method-configuration" in api
