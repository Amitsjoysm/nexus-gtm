# tests/test_stripe_invoice_credits.py
"""Decided 2026-10-09: a subscription paid by emailed invoice receives its plan's credits when the
invoice is PAID. A card subscription (Checkout) still receives them at once."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from tests.test_billing_checkout_api import _seeded_tenant

SECRET = "whsec_credits"


async def _send(client, monkeypatch, event_type: str, obj: dict):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SECRET)
    payload = json.dumps({"id": f"evt_{uuid.uuid4().hex[:12]}", "type": event_type,
                          "data": {"object": obj}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    r = await client.post("/api/billing/webhooks/stripe", content=payload,
                          headers={"Stripe-Signature": f"t={ts},v1={sig}",
                                   "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return r


async def _state(tid: str):
    from sqlalchemy import select

    from nexus.billing.credits import balance
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.billing import BillingPlan, BillingSubscription

    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        launch = await s.get(BillingPlan, "launch")
        return sub, float(await balance(TenantSession(s, tid))), int(launch.included_credits)


def _subscription(tid: str, sub_id: str, *, invoiced: bool) -> dict:
    now = int(time.time())
    meta = {"tenant_id": tid, "plan_id": "launch"}
    if invoiced:
        meta["nexus_credits"] = "on_payment"
    return {"id": sub_id, "object": "subscription", "customer": f"cus_{sub_id}",
            "status": "active", "metadata": meta, "current_period_start": now,
            "current_period_end": now + 30 * 86400}


async def test_an_invoiced_subscription_is_credited_when_the_invoice_is_paid(client, monkeypatch):
    _, tid = await _seeded_tenant(client, slug="ic1", plan_id="free")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "customer.subscription.created",
                _subscription(tid, "sub_inv1", invoiced=True))
    sub, now, _ = await _state(tid)
    assert sub.plan_id == "launch"
    assert now == before                                   # the invoice is unpaid
    assert sub.meta["credits_pending_plan"] == "launch"

    invoice = {"id": "in_inv1", "object": "invoice", "subscription": "sub_inv1",
               "customer": "cus_sub_inv1", "status": "paid", "amount_paid": 9900,
               "billing_reason": "subscription_create", "metadata": {}}
    await _send(client, monkeypatch, "invoice.paid", invoice)
    sub, paid, _ = await _state(tid)
    assert paid == before + credits
    assert "credits_pending_plan" not in sub.meta

    await _send(client, monkeypatch, "invoice.paid", {**invoice, "id": "in_inv1b"})
    assert (await _state(tid))[1] == before + credits          # never twice


async def test_a_card_subscription_is_still_credited_at_once(client, monkeypatch):
    _, tid = await _seeded_tenant(client, slug="ic2", plan_id="free")
    _, before, credits = await _state(tid)
    await _send(client, monkeypatch, "customer.subscription.created",
                _subscription(tid, "sub_card1", invoiced=False))
    assert (await _state(tid))[1] == before + credits
