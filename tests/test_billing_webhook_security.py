# tests/test_billing_webhook_security.py
"""Adversarial tests for the webhook endpoint.

A webhook URL is public and unauthenticated. Every one of these is a way someone with the URL
could try to mark an invoice paid, replay a payment, or learn the signing secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from tests.conftest import auth, signup  # noqa: F401  (client fixture is auto-discovered)

SECRET = "whsec_test_secret_for_unit_tests"


def _sign(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(event_id: str = "evt_1", event_type: str = "payment_intent.succeeded",
           reference: str = "pi_test_1") -> bytes:
    return json.dumps({
        "id": event_id,
        "type": event_type,
        "data": {"object": {"id": reference, "amount": 7900}},
    }).encode()


@pytest.fixture
def webhook_secret(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SECRET)
    return SECRET


# ---- signature ------------------------------------------------------------------------------

def test_valid_signature_is_accepted(webhook_secret):
    from nexus.billing.webhooks import verify_stripe_signature

    body = _event()
    ev = verify_stripe_signature(body, _sign(body), SECRET)
    assert ev.event_id == "evt_1"
    assert ev.event_type == "payment_intent.succeeded"


def test_forged_signature_is_refused(webhook_secret):
    """The core attack: someone POSTs a fabricated 'payment succeeded'."""
    from nexus.billing.webhooks import SignatureError, verify_stripe_signature

    body = _event()
    forged = _sign(body, secret="attacker-guessed-secret")
    with pytest.raises(SignatureError):
        verify_stripe_signature(body, forged, SECRET)


def test_tampered_body_is_refused(webhook_secret):
    """Signature captured from a real event, body swapped for a bigger one."""
    from nexus.billing.webhooks import SignatureError, verify_stripe_signature

    original = _event()
    header = _sign(original)
    tampered = _event(reference="pi_attacker_controlled")
    with pytest.raises(SignatureError):
        verify_stripe_signature(tampered, header, SECRET)


def test_missing_signature_header_is_refused(webhook_secret):
    from nexus.billing.webhooks import SignatureError, verify_stripe_signature

    with pytest.raises(SignatureError):
        verify_stripe_signature(_event(), "", SECRET)


def test_malformed_signature_header_is_refused(webhook_secret):
    from nexus.billing.webhooks import SignatureError, verify_stripe_signature

    body = _event()
    for header in ("garbage", "t=abc,v1=x", "v1=onlysig", "t=123"):
        with pytest.raises(SignatureError):
            verify_stripe_signature(body, header, SECRET)


def test_unconfigured_secret_refuses_everything():
    """An endpoint with no secret can verify nothing. Refusing beats treating it as allowed."""
    from nexus.billing.webhooks import SignatureError, verify_stripe_signature

    body = _event()
    with pytest.raises(SignatureError):
        verify_stripe_signature(body, _sign(body), "")


def test_rotation_accepts_any_valid_v1_signature(webhook_secret):
    """During a secret rotation Stripe sends several v1 entries; one match is enough."""
    from nexus.billing.webhooks import verify_stripe_signature

    body = _event()
    ts = int(time.time())
    good = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1=deadbeef,v1={good}"
    assert verify_stripe_signature(body, header, SECRET).event_id == "evt_1"


# ---- replay ---------------------------------------------------------------------------------

def test_old_event_is_refused_as_a_replay(webhook_secret):
    """A signature stays valid forever unless the timestamp is checked."""
    from nexus.billing.webhooks import StaleWebhookError, verify_stripe_signature

    body = _event()
    old = int(time.time()) - 3600
    with pytest.raises(StaleWebhookError):
        verify_stripe_signature(body, _sign(body, timestamp=old), SECRET)


def test_future_dated_event_is_refused(webhook_secret):
    from nexus.billing.webhooks import StaleWebhookError, verify_stripe_signature

    body = _event()
    future = int(time.time()) + 3600
    with pytest.raises(StaleWebhookError):
        verify_stripe_signature(body, _sign(body, timestamp=future), SECRET)


# ---- endpoint behaviour ---------------------------------------------------------------------

async def test_endpoint_rejects_a_forged_event(client, webhook_secret):
    body = _event()
    r = await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"Stripe-Signature": _sign(body, secret="wrong"),
                 "content-type": "application/json"},
    )
    assert r.status_code == 400
    # Generic message: a precise error tells a prober which check to defeat next.
    assert r.json()["error"] == "invalid_webhook"


async def test_endpoint_rejects_an_unsigned_event(client, webhook_secret):
    body = _event()
    r = await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


async def test_endpoint_applies_a_valid_event_once_then_dedupes(client, webhook_secret):
    """Providers retry deliberately; a replayed success must not apply twice."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingWebhookEvent

    body = _event(event_id="evt_dedupe_1")
    headers = {"Stripe-Signature": _sign(body), "content-type": "application/json"}

    first = await client.post("/api/billing/webhooks/stripe", content=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False

    second = await client.post("/api/billing/webhooks/stripe", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    async with get_sessionmaker()() as s:
        rows = list((await s.scalars(
            select(BillingWebhookEvent).where(BillingWebhookEvent.id == "evt_dedupe_1")
        )).all())
    assert len(rows) == 1        # recorded exactly once


async def test_a_valid_event_for_an_unknown_reference_is_harmless(client, webhook_secret):
    """Someone replaying a well-formed event for an invoice we do not have must not 500."""
    body = _event(event_id="evt_unknown_ref", reference="pi_not_ours")
    r = await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"Stripe-Signature": _sign(body), "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["applied"] is False


async def test_webhook_marks_the_matching_invoice_paid(client, webhook_secret):
    """The real path: a finalized invoice with a stored psp_reference gets confirmed."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice, BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active"))
        await ts.flush()
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        await finalize_invoice(ts, inv.id)
        inv.meta = {**(inv.meta or {}), "psp_reference": "pi_webhook_match"}
        await ts.flush()
        invoice_id = inv.id

    body = _event(event_id="evt_match_1", reference="pi_webhook_match")
    r = await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"Stripe-Signature": _sign(body), "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True

    async with tenant_session(tid) as ts:
        refreshed = await ts.get(BillingInvoice, invoice_id)
        assert refreshed.status == "paid"


async def test_payment_failure_leaves_the_debt_open(client, webhook_secret):
    """A failed payment must not void a real debt; it stays finalized for dunning."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice, BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active"))
        await ts.flush()
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        await finalize_invoice(ts, inv.id)
        inv.meta = {**(inv.meta or {}), "psp_reference": "pi_will_fail"}
        await ts.flush()
        invoice_id = inv.id

    body = json.dumps({
        "id": "evt_fail_1", "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_will_fail",
                            "last_payment_error": {"message": "card_declined"}}},
    }).encode()
    r = await client.post(
        "/api/billing/webhooks/stripe", content=body,
        headers={"Stripe-Signature": _sign(body), "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        refreshed = await ts.get(BillingInvoice, invoice_id)
        assert refreshed.status == "finalized"        # still owed, not voided
        assert "card_declined" in refreshed.meta["last_payment_error"]
