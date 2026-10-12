"""Refunds and chargebacks reach the workspace they concern (2026-10-12).

Found auditing the payment paths for production: the refund and dispute branches looked the event's
OWN id up as a PaymentIntent. A refund event's object is a charge (`ch_...`) and a dispute's is a
dispute (`dp_...`), so neither ever matched an invoice and every refund and chargeback since was
recorded nowhere.

Now:
- a refund or dispute finds its invoice by the PaymentIntent or invoice it names, and a Stripe
  invoice records its PaymentIntent so it can be found; a payment we never recorded is looked up at
  Stripe by its charge;
- a refund is RECORDED, nothing more: refunding is an operator's decision and so is what the
  customer keeps;
- a dispute or an early fraud warning HOLDS the credits the payment bought (the money has already
  left: Stripe debits a disputed amount at once), capped at what is unspent, once per dispute;
- a dispute won releases the hold; one lost keeps it.
"""
from __future__ import annotations

import time

from tests.test_billing_checkout_api import _seeded_tenant
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send, _state


async def _grant(tid: str, amount: float) -> None:
    from nexus.billing.credits import grant_credits
    from tests.conftest import tenant_session

    async with tenant_session(tid) as ts:
        await grant_credits(ts, amount, idempotency_key=f"test:{tid}:{amount}:{time.time()}")


async def _paid_invoice(client, monkeypatch, tid: str, *, n: int) -> None:
    """Stripe raised and collected the subscription's invoice for Launch."""
    await _send(client, monkeypatch, "invoice.paid", {
        "id": f"in_{n}", "object": "invoice", "subscription": f"sub_d{n}",
        "customer": f"cus_{tid[:6]}", "payment_intent": f"pi_{n}", "charge": f"ch_{n}",
        "amount_paid": 9900, "currency": "usd", "status": "paid",
        "metadata": {"tenant_id": tid}})


async def _invoice(tid: str, psp_invoice_id: str):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(BillingInvoice).where(
            BillingInvoice.tenant_id == tid))).all()
        return next(r for r in rows
                    if (r.psp_invoice_id or (r.meta or {}).get("psp_invoice_id")) == psp_invoice_id)


def _dispute(n: int, status: str = "needs_response") -> dict:
    return {"id": f"dp_{n}", "object": "dispute", "charge": f"ch_{n}", "payment_intent": f"pi_{n}",
            "amount": 9900, "currency": "usd", "reason": "fraudulent", "status": status}


async def test_a_refund_is_recorded_on_the_invoice_and_changes_no_credits(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr1")
    await _follow(tid, "sub_d1")
    await _paid_invoice(client, monkeypatch, tid, n=1)
    _, before, _ = await _state(tid)

    await _send(client, monkeypatch, "charge.refunded", {
        "id": "ch_1", "object": "charge", "payment_intent": "pi_1", "invoice": "in_1",
        "customer": f"cus_{tid[:6]}", "amount": 9900, "amount_refunded": 9900, "refunded": True})
    inv = await _invoice(tid, "in_1")
    assert inv.meta["amount_refunded"] == 9900 and inv.meta["refunded_at"]
    assert (await _state(tid))[1] == before


async def test_a_dispute_holds_the_credits_the_payment_bought(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr2")
    await _follow(tid, "sub_d2")
    await _paid_invoice(client, monkeypatch, tid, n=2)
    await _grant(tid, 5000)
    _, before, launch_credits = await _state(tid)

    await _send(client, monkeypatch, "charge.dispute.created", _dispute(2))
    sub, after, _ = await _state(tid)
    assert after == before - launch_credits
    assert (await _invoice(tid, "in_2")).meta["dispute_reason"] == "fraudulent"
    assert sub.meta["open_disputes"] == ["dp_2"]

    # Delivered again under another event id: held once.
    await _send(client, monkeypatch, "charge.dispute.created", _dispute(2))
    assert (await _state(tid))[1] == after


async def test_a_hold_never_takes_more_than_is_unspent(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr3", plan_id="free")
    await _follow(tid, "sub_d3")
    await _paid_invoice(client, monkeypatch, tid, n=3)
    _, before, credits = await _state(tid)
    assert 0 < before < credits                  # the free tier's leftovers, less than Launch's
    await _send(client, monkeypatch, "charge.dispute.created", _dispute(3))
    assert (await _state(tid))[1] == 0           # held what was there, never below zero


async def test_a_won_dispute_releases_the_hold_and_a_lost_one_keeps_it(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr4")
    await _follow(tid, "sub_d4")
    await _paid_invoice(client, monkeypatch, tid, n=4)
    await _paid_invoice(client, monkeypatch, tid, n=5)
    await _grant(tid, 9000)
    _, start, credits = await _state(tid)

    await _send(client, monkeypatch, "charge.dispute.created", _dispute(4))
    await _send(client, monkeypatch, "charge.dispute.created", _dispute(5))
    assert (await _state(tid))[1] == start - 2 * credits

    await _send(client, monkeypatch, "charge.dispute.closed", _dispute(4, "won"))
    await _send(client, monkeypatch, "charge.dispute.closed", _dispute(5, "lost"))
    sub, end, _ = await _state(tid)
    assert end == start - credits
    assert sub.meta["open_disputes"] == []
    assert sub.meta["lost_disputes"] == ["dp_5"]


async def test_an_early_fraud_warning_holds_the_credits_too(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr5")
    await _follow(tid, "sub_d6")
    await _paid_invoice(client, monkeypatch, tid, n=6)
    await _grant(tid, 5000)
    _, before, credits = await _state(tid)
    await _send(client, monkeypatch, "radar.early_fraud_warning.created", {
        "id": "issfr_6", "object": "radar.early_fraud_warning", "charge": "ch_6",
        "payment_intent": "pi_6", "fraud_type": "stolen_card", "actionable": True})
    sub, after, _ = await _state(tid)
    assert after == before - credits
    assert sub.meta["fraud_warnings"] == ["issfr_6"]


async def test_a_dispute_on_a_payment_never_recorded_is_found_through_its_charge(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="dr6")
    await _follow(tid, "sub_d7")
    await _grant(tid, 5000)
    _, before, credits = await _state(tid)
    noop_provider.charge_objects["ch_7"] = {"id": "ch_7", "customer": f"cus_{tid[:6]}",
                                            "invoice": "in_7", "payment_intent": "pi_7"}
    await _send(client, monkeypatch, "charge.dispute.created", _dispute(7))
    assert (await _state(tid))[1] == before - credits


def test_the_risk_events_are_handled():
    from nexus.billing.webhooks import HANDLED_EVENTS

    for event in ("charge.dispute.closed", "radar.early_fraud_warning.created"):
        assert event in HANDLED_EVENTS
