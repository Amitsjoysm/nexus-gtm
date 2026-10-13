"""Why Stripe Checkout says "no valid payment methods", before a customer finds out (2026-10-12).

Reported: "Continue to checkout" opened Stripe's page and it said "There are no valid payment methods
available for this session. Please contact the merchant." Our session names no payment method
types, so Stripe chooses from the ACCOUNT's own settings when the page renders, and refuses when
none fits the session (a subscription, its currency, the buyer's country). The session itself is
created without complaint, so nothing on our side saw it.

`checkout_readiness` reads the account and names the cause:
- charges not enabled (an account Stripe has not finished activating), blocking;
- the card capability not active, blocking;
- cards switched off in the default payment method configuration, blocking;
- an Indian Stripe account selling in another currency: Stripe lets Indian buyers pay an Indian
  business only in INR, so a buyer in India sees exactly that message. A warning, because buyers
  outside India can pay.
Checkout refuses on a blocking cause with a sentence instead of a dead page, and goes ahead when
the check itself cannot run. An Indian account's Checkout collects the billing address, which
Stripe requires for India's international payments.
"""
from __future__ import annotations

import pytest

from nexus.billing.payments import PaymentError, StripePaymentProvider

HEALTHY = {"id": "acct_1", "country": "US", "default_currency": "usd", "charges_enabled": True,
           "capabilities": {"card_payments": "active"}, "requirements": {"currently_due": []}}
CONFIGS = {"data": [{"id": "pmc_1", "is_default": True, "active": True,
                     "card": {"available": True, "display_preference": {"value": "on"}}}]}


class FakeStripe(StripePaymentProvider):
    made = 0

    def __init__(self, account=None, configs=None, *, config_error=None, account_error=None,
                 mode="live"):
        # A key of its own: the account's country and id are cached per key, as in production.
        # Live by default: what blocks a live account is a warning in test mode.
        FakeStripe.made += 1
        super().__init__(f"sk_{mode}_fake{FakeStripe.made}")
        self.account, self.configs = account or HEALTHY, configs or CONFIGS
        self.config_error, self.account_error = config_error, account_error
        self.posted: list[tuple[str, dict]] = []

    async def _get(self, path):
        if path.startswith("/account"):
            if self.account_error:
                raise self.account_error
            return self.account
        if path.startswith("/payment_method_configurations"):
            if self.config_error:
                raise self.config_error
            return self.configs
        return {}

    async def _post(self, path, form, idempotency_key=""):
        self.posted.append((path, dict(form)))
        return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}


@pytest.fixture(autouse=True)
def fresh_cache():
    from nexus.billing import checkout_readiness, payments

    checkout_readiness.clear_cache()
    yield
    checkout_readiness.clear_cache()
    payments._account_countries.clear()


async def _ready(provider, currencies=("USD",)):
    from nexus.billing.checkout_readiness import checkout_readiness

    return await checkout_readiness(provider, currencies=set(currencies))


async def test_a_ready_account_has_nothing_to_say():
    r = await _ready(FakeStripe())
    assert r.ok and r.blocking == [] and r.warnings == []


async def test_an_account_stripe_has_not_activated_blocks_checkout():
    account = {**HEALTHY, "charges_enabled": False,
               "requirements": {"currently_due": ["business_profile.url", "external_account"]}}
    r = await _ready(FakeStripe(account))
    assert not r.ok and "charges" in r.blocking[0] and "2" in r.blocking[0]


async def test_an_inactive_card_capability_blocks_checkout():
    r = await _ready(FakeStripe({**HEALTHY, "capabilities": {"card_payments": "inactive"}}))
    assert not r.ok and any("inactive" in b for b in r.blocking)


async def test_an_account_not_yet_activated_still_takes_test_payments():
    """Stripe's test mode takes payments before an account is activated. Refusing there stopped
    a fresh account's checkout for a reason that only applies to live money (2026-10-13)."""
    fresh = {**HEALTHY, "charges_enabled": False, "capabilities": {}}
    r = await _ready(FakeStripe(fresh, mode="test"))
    assert r.ok and any("charges" in w for w in r.warnings)
    r = await _ready(FakeStripe({**HEALTHY, "capabilities": {"card_payments": "inactive"}},
                                mode="test"))
    assert r.ok and any("inactive" in w for w in r.warnings)
    assert not (await _ready(FakeStripe(fresh))).ok          # live: still refused


async def test_cards_switched_off_in_the_payment_method_settings_block_checkout():
    configs = {"data": [{"id": "pmc_1", "is_default": True, "active": True,
                         "card": {"available": True, "display_preference": {"value": "off"}}}]}
    r = await _ready(FakeStripe(configs=configs))
    assert not r.ok and any("Payment methods" in b for b in r.blocking)


async def test_an_indian_account_selling_in_dollars_is_warned_about_buyers_in_india():
    r = await _ready(FakeStripe({**HEALTHY, "country": "IN", "default_currency": "inr"}))
    assert r.ok                                   # buyers outside India can pay
    assert r.warnings and "INR" in r.warnings[0] and "USD" in r.warnings[0]
    assert (await _ready(FakeStripe({**HEALTHY, "country": "IN"}), ("INR",))).warnings == []


async def test_settings_a_restricted_key_cannot_read_are_a_note_not_a_failure():
    r = await _ready(FakeStripe(config_error=PaymentError("403 permission")))
    assert r.ok and any("payment method settings" in w for w in r.warnings)


async def test_an_indian_accounts_checkout_collects_the_billing_address():
    provider = FakeStripe({**HEALTHY, "country": "IN"})
    await provider.create_checkout_session(tenant_id="t1", plan_id="launch", price_id="price_1",
                                           success_url="https://x/s", cancel_url="https://x/c")
    form = provider.posted[-1][1]
    assert form["billing_address_collection"] == "required"

    us = FakeStripe()
    await us.create_checkout_session(tenant_id="t1", plan_id="launch", price_id="price_1",
                                     success_url="https://x/s", cancel_url="https://x/c")
    assert "billing_address_collection" not in us.posted[-1][1]


async def _checkout(client, monkeypatch, provider):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.payments import set_payment_provider
    from nexus.billing.plans import sync_plans
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    set_payment_provider(provider)
    token = await signup(client, slug="chk1", email="owner@chk1.io")
    return await client.post("/api/billing/checkout", headers=auth(token),
                             json={"plan_id": "launch"})


async def test_checkout_refuses_with_a_sentence_when_the_account_cannot_take_payment(
        client, monkeypatch):
    from nexus.billing.payments import set_payment_provider

    provider = FakeStripe({**HEALTHY, "charges_enabled": False})
    try:
        r = await _checkout(client, monkeypatch, provider)
        assert r.status_code == 409, r.text
        assert "Nothing was charged" in r.json()["detail"]
        assert not any(path == "/checkout/sessions" for path, _ in provider.posted)
    finally:
        set_payment_provider(None)


async def test_checkout_goes_ahead_when_the_check_itself_cannot_run(client, monkeypatch):
    from nexus.billing import checkout_readiness
    from nexus.billing.payments import set_payment_provider

    async def broken(*args, **kwargs):
        raise RuntimeError("stripe timed out")

    monkeypatch.setattr(checkout_readiness, "checkout_readiness", broken)
    provider = FakeStripe()
    try:
        r = await _checkout(client, monkeypatch, provider)
        assert r.status_code == 200, r.text
        assert any(path == "/checkout/sessions" for path, _ in provider.posted)
    finally:
        set_payment_provider(None)


async def test_the_payments_tab_checks_a_credentials_own_account(client, monkeypatch):
    from tests.conftest import auth
    from tests.test_admin_permissions import _bootstrap_admin
    from tests.test_stripe_switch_setup import _credential

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        if path.startswith("/account"):
            return {**HEALTHY, "country": "IN", "charges_enabled": False}
        return CONFIGS

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    boss = await _bootstrap_admin(client, monkeypatch, "chkcred")
    cid = await _credential()
    r = await client.post(f"/api/admin/payment-credentials/{cid}/checkout-check",
                          headers=auth(boss))
    assert r.status_code == 200, r.text
    body = r.json()
    # The credential's key is a test key, so an unactivated account is a warning there.
    assert body["ok"] is True and body["country"] == "IN"
    assert any("charges" in w for w in body["warnings"])


def test_the_payments_tab_offers_the_check():
    from pathlib import Path

    tab = (Path(__file__).resolve().parents[1]
           / "frontend/src/pages/admin/PaymentsTab.tsx").read_text(encoding="utf-8")
    assert "checkPaymentCheckout" in tab and "Check checkout" in tab
