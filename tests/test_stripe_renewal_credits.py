"""A Stripe-billed renewal is credited when Stripe is paid for it, once (2026-10-12).

Found auditing the payment paths for production. `roll_period` granted the new period's credits at
OUR period boundary, before Stripe had even tried the card, so a renewal that was never paid had
already been spent. And when Stripe's own `customer.subscription.updated` moved the row's period
forward first, which it does at the same moment, the roll never ran for that period and the
renewal was not credited at all.

Now the paid renewal invoice (`billing_reason: subscription_cycle`) grants the plan's credits,
keyed by the invoice, and the roll grants nothing for a workspace Stripe bills.
"""
from __future__ import annotations

from tests.test_billing_checkout_api import _seeded_tenant, noop_provider  # noqa: F401
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send, _state


def _invoice(tid: str, n: str, sub: str, reason: str = "subscription_cycle") -> dict:
    return {"id": f"in_{n}", "object": "invoice", "subscription": sub, "billing_reason": reason,
            "customer": f"cus_{tid[:6]}", "payment_intent": f"pi_{n}", "amount_paid": 9900,
            "currency": "usd", "status": "paid", "metadata": {"tenant_id": tid}}


async def test_a_paid_renewal_is_credited_once(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="rn1")
    await _follow(tid, "sub_rn1")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "rn1", "sub_rn1"))
    assert (await _state(tid))[1] == before + credits
    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "rn1", "sub_rn1"))  # again
    assert (await _state(tid))[1] == before + credits

    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "rn1b", "sub_rn1"))  # next one
    assert (await _state(tid))[1] == before + 2 * credits


async def test_an_unpaid_renewal_and_a_first_invoice_are_not_renewal_credits(
        client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="rn2")
    await _follow(tid, "sub_rn2")
    _, before, _ = await _state(tid)

    failed = {**_invoice(tid, "rn2", "sub_rn2"), "status": "open", "amount_paid": 0}
    await _send(client, monkeypatch, "invoice.payment_failed", failed)
    # The first invoice paid for the purchase itself, which Checkout already credited.
    await _send(client, monkeypatch, "invoice.paid",
                _invoice(tid, "rn2c", "sub_rn2", reason="subscription_create"))
    # A subscription this workspace does not follow renews nothing here.
    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "rn2o", "sub_other"))
    assert (await _state(tid))[1] == before


def _next_month(monkeypatch):
    """Run the roll 40 days from now: its grant is keyed by the calendar month, and signup has
    already used this one, so a roll inside it would grant nothing whatever the rule."""
    from datetime import timedelta

    from nexus.core.db import utcnow

    later = utcnow() + timedelta(days=40)
    monkeypatch.setattr("nexus.billing.subscriptions.utcnow", lambda: later)


async def test_the_period_roll_credits_nothing_for_a_workspace_stripe_bills(
        client, monkeypatch, noop_provider):
    from datetime import timedelta

    from nexus.billing.subscriptions import roll_period
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    _, tid = await _seeded_tenant(client, slug="rn3")
    await _follow(tid, "sub_rn3")
    _, before, _ = await _state(tid)
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.current_period_start = utcnow() - timedelta(days=31)
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        _next_month(monkeypatch)
        assert await roll_period(ts) is True
    assert (await _state(tid))[1] == before


async def test_the_period_roll_still_credits_a_workspace_billed_here(
        client, monkeypatch, noop_provider):
    from datetime import timedelta

    from nexus.billing.credits import balance
    from nexus.billing.subscriptions import roll_period
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription
    from tests.conftest import tenant_session

    _, tid = await _seeded_tenant(client, slug="rn4", plan_id="free")
    async with tenant_session(tid) as ts:
        before = await balance(ts)
        sub = await ts.first(BillingSubscription)
        sub.current_period_start = utcnow() - timedelta(days=31)
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        _next_month(monkeypatch)
        assert await roll_period(ts) is True
        assert await balance(ts) > before
