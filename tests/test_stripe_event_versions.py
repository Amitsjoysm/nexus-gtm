"""Stripe events read the same whatever API version the webhook endpoint renders them in (2026-10-21).

Stripe renders a webhook in its ENDPOINT's API version. Create webhook in Stripe pins
`StripePaymentProvider.API_VERSION` (2024-06-20); an endpoint added by hand in Stripe's Dashboard
takes the account's default, and since 2025-03-31 that version moves two fields the handlers read.
Measured on the local stack's test account (2026-06-24.dahlia) by paying real test-mode Checkouts:

- a subscription's billing period is only on its items, so the period never synced: a workspace that
  bought Launch (annual) kept the one-month period it had on Free;
- an invoice's subscription is only under `parent.subscription_details`, so a paid renewal would never
  be credited, and an invoice from a subscription moved to another account passed the guard that
  ignores it.

The shapes below are the ones Stripe sent, trimmed to the fields read.
"""
from __future__ import annotations

import time
from datetime import timezone

from tests.test_billing_checkout_api import _seeded_tenant
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send, _state

YEAR = 365 * 86400


def _invoice(tid: str, n: str, sub: str, reason: str = "subscription_cycle") -> dict:
    """An invoice as a 2025-03-31-or-later endpoint sends it: no top-level `subscription`,
    `payment_intent` or `charge`, and the subscription's metadata under `parent`."""
    return {"id": f"in_{n}", "object": "invoice", "billing_reason": reason,
            "customer": f"cus_{tid[:6]}", "amount_paid": 9900, "currency": "usd", "status": "paid",
            "metadata": {},
            "parent": {"type": "subscription_details", "quote_details": None,
                       "subscription_details": {"subscription": sub,
                                                "metadata": {"tenant_id": tid, "plan_id": "launch"}}}}


def _subscription(tid: str, psp: str, *, plan_id: str, start: int, end: int, **top) -> dict:
    """A subscription as a 2025-03-31-or-later endpoint sends it: the period only on its items."""
    return {"id": psp, "object": "subscription", "customer": f"cus_{tid[:6]}", "status": "active",
            "cancel_at_period_end": False, "metadata": {"tenant_id": tid, "plan_id": plan_id},
            "items": {"object": "list", "data": [
                {"id": f"si_{psp}", "object": "subscription_item",
                 "current_period_start": start, "current_period_end": end,
                 "price": {"recurring": {"interval": "year"}}}]},
            **top}


def _epoch_of(value) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


async def test_a_renewal_in_the_new_shape_is_credited_once(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="ev1")
    await _follow(tid, "sub_ev1")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "ev1", "sub_ev1"))
    assert (await _state(tid))[1] == before + credits
    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "ev1", "sub_ev1"))  # again
    assert (await _state(tid))[1] == before + credits
    # Another subscription's renewal is still not this workspace's.
    await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "ev1o", "sub_other"))
    assert (await _state(tid))[1] == before + credits


async def test_the_billing_period_comes_from_the_items(client, monkeypatch, noop_provider):
    _, tid = await _seeded_tenant(client, slug="ev2", plan_id="launch-annual")
    await _follow(tid, "sub_ev2", plan_id="launch-annual")
    now = int(time.time())

    await _send(client, monkeypatch, "customer.subscription.updated",
                _subscription(tid, "sub_ev2", plan_id="launch-annual", start=now, end=now + YEAR))
    sub = (await _state(tid))[0]
    assert abs(_epoch_of(sub.current_period_start) - now) < 2
    assert abs(_epoch_of(sub.current_period_end) - (now + YEAR)) < 2


async def test_a_pinned_endpoints_period_is_read_as_before(client, monkeypatch, noop_provider):
    """Where the subscription carries its own period, that is the one used: nothing changes for an
    endpoint made by Create webhook in Stripe."""
    _, tid = await _seeded_tenant(client, slug="ev3")
    await _follow(tid, "sub_ev3")
    now = int(time.time())
    month = now + 30 * 86400

    await _send(client, monkeypatch, "customer.subscription.updated",
                _subscription(tid, "sub_ev3", plan_id="launch", start=now, end=now + YEAR,
                              current_period_start=now, current_period_end=month))
    assert abs(_epoch_of((await _state(tid))[0].current_period_end) - month) < 2


async def test_an_old_accounts_invoice_in_the_new_shape_is_still_ignored(
        client, monkeypatch, noop_provider):
    """An invoice from a subscription moved to another Stripe account must not touch the one that
    replaced it, here by delivering credits the workspace is waiting to pay for."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.models.billing import BillingSubscription

    _, tid = await _seeded_tenant(client, slug="ev4")
    await _follow(tid, "sub_ev4_new")
    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        sub.meta = {**(sub.meta or {}), "credits_pending_plan": "launch",
                    "previous_psp_subscriptions": [{"id": "sub_ev4_old", "reason": "carried"}]}
        await s.commit()
    _, before, _ = await _state(tid)

    r = await _send(client, monkeypatch, "invoice.paid", _invoice(tid, "ev4", "sub_ev4_old"))
    assert r.json().get("note") == "subscription moved to another Stripe account"
    after, balance, _ = await _state(tid)
    assert balance == before
    assert (after.meta or {}).get("credits_pending_plan") == "launch"


def test_the_reading_fills_only_what_is_missing_and_leaves_the_payload_alone():
    from nexus.billing.webhooks import _in_pinned_shape

    new = _invoice("t" * 32, "x", "sub_x")
    read = _in_pinned_shape("invoice.paid", new)
    assert read["subscription"] == "sub_x"
    assert "subscription" not in new  # the payload Stripe sent is not rewritten

    pinned = {"id": "in_p", "object": "invoice", "subscription": "sub_p",
              "parent": {"subscription_details": {"subscription": "sub_other"}}}
    assert _in_pinned_shape("invoice.paid", pinned)["subscription"] == "sub_p"

    no_items = {"id": "sub_n", "object": "subscription", "status": "active"}
    assert _in_pinned_shape("customer.subscription.updated", no_items) == no_items
    # A one-off invoice has no subscription in either version.
    one_off = {"id": "in_o", "object": "invoice", "parent": None}
    assert not _in_pinned_shape("invoice.paid", one_off).get("subscription")
