# tests/test_stripe_http_hardening.py
"""The HTTP layer under every money call.

Three properties that a payments client cannot ship without, none of which were present:

* **A pinned API version.** Without ``Stripe-Version`` the account's default applies, so Stripe
  can change a response shape under a running deployment with no deploy on our side. Every field
  this module reads — ``status``, ``current_period_end``, ``hosted_invoice_url`` — is a shape.
* **Retry on transient failures.** Stripe rate-limits, and a 429 or a 502 raised straight out of
  `_post` becomes a lost charge or a 500 in front of a customer. Safe to retry precisely because
  the idempotency keys are already threaded through.
* **A retry must NOT fire on a 4xx that will never succeed.** `card_declined` retried three times
  is three declines and a slower error.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.billing.payments import PaymentError, StripePaymentProvider


def _provider(handler, **kw) -> StripePaymentProvider:
    p = StripePaymentProvider("sk_test_x", **kw)
    p._transport = httpx.MockTransport(handler)
    return p


async def test_every_request_pins_the_api_version():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"id": "obj_1"})

    p = _provider(handler)
    await p._post("/customers", {"email": "a@b.c"})
    await p._get("/customers/cus_1")

    assert len(seen) == 2
    for headers in seen:
        assert headers.get("stripe-version"), "no Stripe-Version header; the account default applies"
        assert headers["stripe-version"] == StripePaymentProvider.API_VERSION


async def test_a_rate_limit_is_retried_and_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"id": "pi_1"})

    p = _provider(handler, max_retries=3, retry_base_delay_s=0)
    out = await p._post("/payment_intents", {"amount": "500"}, idempotency_key="k")
    assert out["id"] == "pi_1"
    assert calls["n"] == 3, "the 429 was not retried"


async def test_a_server_error_is_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502 if calls["n"] == 1 else 200, json={"id": "in_1"})

    p = _provider(handler, max_retries=3, retry_base_delay_s=0)
    assert (await p._post("/invoices", {}))["id"] == "in_1"
    assert calls["n"] == 2


async def test_a_card_decline_is_not_retried():
    """A 402 is a final answer. Retrying is three declines and a slower error."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(402, json={"error": {"code": "card_declined"}})

    p = _provider(handler, max_retries=3, retry_base_delay_s=0)
    with pytest.raises(PaymentError):
        await p._post("/payment_intents", {"amount": "500"}, idempotency_key="k")
    assert calls["n"] == 1, "a terminal 4xx must not be retried"


async def test_retries_reuse_the_idempotency_key():
    """Otherwise the retry is a second, independent charge — the exact double-bill the key exists
    to prevent."""
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("idempotency-key", ""))
        return httpx.Response(429 if len(keys) < 2 else 200, json={"id": "pi_1"})

    p = _provider(handler, max_retries=3, retry_base_delay_s=0)
    await p._post("/payment_intents", {"amount": "500"}, idempotency_key="charge-42")
    assert keys == ["charge-42", "charge-42"]


async def test_exhausted_retries_raise_rather_than_returning_a_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    p = _provider(handler, max_retries=2, retry_base_delay_s=0)
    with pytest.raises(PaymentError):
        await p._post("/customers", {})
