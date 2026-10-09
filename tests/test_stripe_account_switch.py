# tests/test_stripe_account_switch.py
"""After the Stripe key changes account, checkout uses ids from the NEW account (reported
2026-10-09 as "product id not available" after adding a new Stripe key)."""
from __future__ import annotations

import pytest

from tests.conftest import auth
from tests.stripe_fakes import TwoAccountStripe
from tests.test_billing_checkout_api import _seeded_tenant

BODY = {"plan_id": "accelerate", "success_url": "https://app/ok", "cancel_url": "https://app/no"}


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def test_checkout_after_an_account_switch_uses_the_new_accounts_ids(client, stripe):
    token, _ = await _seeded_tenant(client, slug="acsw1")
    assert (await client.post("/api/billing/checkout", json=BODY,
                              headers=auth(token))).status_code == 200
    first = stripe.checkout_sessions[-1]
    assert first["price_id"].startswith("price_acct_old")
    assert first["customer_id"].startswith("cus_acct_old")

    stripe.account = "acct_new"
    r = await client.post("/api/billing/checkout", json=BODY, headers=auth(token))
    assert r.status_code == 200, r.text
    second = stripe.checkout_sessions[-1]
    assert second["price_id"].startswith("price_acct_new")
    assert second["customer_id"].startswith("cus_acct_new")


async def test_checkout_inside_one_account_reuses_everything(client, stripe):
    token, _ = await _seeded_tenant(client, slug="acsw2")
    for _ in range(3):
        assert (await client.post("/api/billing/checkout", json=BODY,
                                  headers=auth(token))).status_code == 200
    assert len(stripe.ensured) == 1
    assert len({s["customer_id"] for s in stripe.checkout_sessions}) == 1
