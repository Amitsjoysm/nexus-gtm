# tests/test_stripe_checkout_and_events.py
"""Commerce features an enterprise buyer asks for on the first call, and the events that carry
the answers.

Three gaps, each small in code and large in consequence:

* **No promotion codes.** Sales could not discount. Every deal that needed one had to become an
  admin-managed custom plan, which is the expensive path and leaves self-serve unusable.
* **No tax.** `automatic_tax` is a hard requirement for selling into the EU and UK. It must be
  opt-in: Stripe rejects the parameter outright on an account without Stripe Tax configured, so
  defaulting it on would break checkout for every existing deployment.
* **`invoice.upcoming` and `customer.subscription.trial_will_end` were unhandled**, so the two
  events that let a customer be warned before money moves changed nothing. `invoice.upcoming` is
  also how several jurisdictions expect a renewal notice to be triggered.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.billing.payments import StripePaymentProvider


def _capture() -> tuple[StripePaymentProvider, list]:
    posted: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode() or ""))
        posted.append((request.url.path, body))
        return httpx.Response(200, json={"id": "cs_1", "url": "https://checkout/x"})

    p = StripePaymentProvider("sk_test_x")
    p._transport = httpx.MockTransport(handler)
    return p, posted


async def test_checkout_accepts_promotion_codes():
    p, posted = _capture()
    await p.create_checkout_session(
        tenant_id="t1", plan_id="growth", price_id="price_1",
        success_url="https://a/ok", cancel_url="https://a/no",
    )
    _, form = posted[0]
    assert form.get("allow_promotion_codes") == "true", (
        "the coupon field is absent from Checkout, so sales cannot discount at all"
    )


async def test_tax_is_off_unless_the_deployment_turns_it_on(monkeypatch):
    """Stripe errors on `automatic_tax` when Stripe Tax is not configured, so the default must be
    off or this breaks checkout everywhere it is not set up."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_automatic_tax", False)
    p, posted = _capture()
    await p.create_checkout_session(
        tenant_id="t1", plan_id="growth", price_id="price_1",
        success_url="https://a/ok", cancel_url="https://a/no",
    )
    _, form = posted[0]
    assert "automatic_tax[enabled]" not in form


async def test_tax_is_requested_when_the_deployment_turns_it_on(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_automatic_tax", True)
    p, posted = _capture()
    await p.create_checkout_session(
        tenant_id="t1", plan_id="growth", price_id="price_1",
        success_url="https://a/ok", cancel_url="https://a/no",
    )
    _, form = posted[0]
    assert form.get("automatic_tax[enabled]") == "true"
    # Stripe needs an address to compute a rate, and will not collect one unless asked.
    assert form.get("billing_address_collection") == "required"
    assert form.get("customer_update[address]") == "auto" or "customer" not in form


def test_the_advance_warning_events_are_handled():
    from nexus.billing.webhooks import INVOICE_EVENTS, SUBSCRIPTION_EVENTS

    assert "invoice.upcoming" in INVOICE_EVENTS, (
        "nothing reacts to the renewal notice event"
    )
    assert "customer.subscription.trial_will_end" in SUBSCRIPTION_EVENTS, (
        "a trial ends and charges with no warning path"
    )


async def test_an_upcoming_invoice_records_the_amount_without_creating_one():
    """It is a preview. Writing a real invoice row from it would bill the customer twice."""
    from sqlalchemy import select

    from nexus.billing.webhooks import VerifiedEvent, handle_event
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice, BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active",
                                   psp_subscription_id="sub_x"))
        await ts.flush()

    event = VerifiedEvent(
        event_id="evt_up_1", event_type="invoice.upcoming",
        payload={"id": "evt_up_1", "type": "invoice.upcoming",
                 "data": {"object": {"id": "in_preview", "subscription": "sub_x",
                                     "amount_due": 19900,
                                     "next_payment_attempt": 1790000000}}},
        digest="d",
    )
    async with get_platform_sessionmaker()() as session:
        out = await handle_event(session, event)
        await session.commit()
        invoices = (await session.scalars(select(BillingInvoice))).all()

    assert out.get("applied") is True
    assert invoices == [], "a preview was written as a real invoice"

    async with tenant_session(tid) as ts:
        sub = (await ts.session.scalars(select(BillingSubscription))).first()
        assert sub.meta.get("upcoming_amount_due_cents") == 19900
