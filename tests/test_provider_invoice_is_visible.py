# tests/test_provider_invoice_is_visible.py
"""A subscription paid at Stripe must appear on the customer's own invoices page.

Reported from production alongside the missing credits: the payment succeeded and no invoice
appeared anywhere in the product.

It is not lost — it never existed on our side. Stripe raises and charges the invoice for a hosted
subscription; we do not. `_apply_invoice_event` then looks for a LOCAL `billing_invoices` row by
`psp_reference` or `psp_invoice_id`, finds none (we never created one), and takes the "no local
invoice" branch: it stamps `psp_latest_invoice_id` onto the subscription and returns. Meanwhile
`GET /billing/invoices` reads `billing_invoices`, which is populated only by invoices WE raise —
so the one document proving what the customer paid is the one thing the product cannot show them.

The fix records the provider's invoice as a local row so it is visible, carrying the hosted URL
and PDF link Stripe already generated. Our rating still prices nothing here: the provider charged
it, and the amount is copied rather than recomputed.

`period_key` is namespaced `stripe:<id>` rather than the calendar period, because
`billing_invoices` carries `UniqueConstraint(tenant_id, period_key)` and a usage invoice for the
same month would collide with it.
"""
from __future__ import annotations


from tests.conftest import make_tenant, tenant_session


def _event(event_id: str, event_type: str, obj: dict):
    from nexus.billing.webhooks import VerifiedEvent

    return VerifiedEvent(
        event_id=event_id, event_type=event_type,
        payload={"id": event_id, "type": event_type, "data": {"object": obj}},
        digest="d",
    )


async def _tenant_with_stripe_sub():
    from nexus.models.billing import BillingSubscription

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(
            plan_id="launch", status="active",
            psp_subscription_id="sub_live_1", psp_customer_id="cus_live_1",
        ))
        await ts.flush()
    return tid


def _paid_invoice_object() -> dict:
    return {
        "id": "in_stripe_1",
        "subscription": "sub_live_1",
        "customer": "cus_live_1",
        "number": "ABCD-0001",
        "status": "paid",
        "amount_due": 9900,
        "amount_paid": 9900,
        "currency": "usd",
        "hosted_invoice_url": "https://invoice.stripe.com/i/abc",
        "invoice_pdf": "https://pay.stripe.com/invoice/abc/pdf",
    }


async def _invoices(ts):
    from sqlalchemy import select

    from nexus.models.billing import BillingInvoice

    return list((await ts.session.scalars(select(BillingInvoice))).all())


async def test_a_paid_stripe_invoice_becomes_visible_to_the_customer():
    from nexus.billing.webhooks import handle_event
    from nexus.core.db import get_platform_sessionmaker

    tid = await _tenant_with_stripe_sub()
    async with get_platform_sessionmaker()() as session:
        outcome = await handle_event(
            session, _event("evt_inv_1", "invoice.paid", _paid_invoice_object())
        )
        await session.commit()

    assert outcome.get("applied") is True
    async with tenant_session(tid) as ts:
        rows = await _invoices(ts)
    assert len(rows) == 1, (
        "a successful payment produced no invoice the customer can see — which is exactly what "
        "was reported"
    )
    inv = rows[0]
    assert inv.status == "paid"
    assert inv.total_cents == 9900, "the amount was recomputed rather than copied from Stripe"
    assert inv.number == "ABCD-0001"
    assert (inv.meta or {}).get("hosted_invoice_url") == "https://invoice.stripe.com/i/abc"
    assert (inv.meta or {}).get("invoice_pdf_url") == "https://pay.stripe.com/invoice/abc/pdf"


async def test_the_same_invoice_arriving_twice_makes_one_row():
    """Stripe sends `invoice.finalized` then `invoice.paid` for the same document, and retries
    both. Two rows would show the customer the same charge twice."""
    from nexus.billing.webhooks import handle_event
    from nexus.core.db import get_platform_sessionmaker

    tid = await _tenant_with_stripe_sub()
    obj = _paid_invoice_object()
    async with get_platform_sessionmaker()() as session:
        await handle_event(session, _event("evt_a", "invoice.finalized", {**obj, "status": "open"}))
        await handle_event(session, _event("evt_b", "invoice.paid", obj))
        await handle_event(session, _event("evt_c", "invoice.paid", obj))
        await session.commit()

    async with tenant_session(tid) as ts:
        rows = await _invoices(ts)
    assert len(rows) == 1, f"one Stripe invoice produced {len(rows)} rows"
    assert rows[0].status == "paid", "the later paid event did not update the finalized row"


async def test_it_does_not_collide_with_a_usage_invoice_for_the_same_month():
    """`billing_invoices` is unique on (tenant_id, period_key). A calendar key would make a
    Stripe subscription invoice and our own usage invoice for the same month mutually exclusive."""
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key
    from nexus.billing.webhooks import handle_event
    from nexus.core.db import get_platform_sessionmaker, utcnow

    tid = await _tenant_with_stripe_sub()
    pk = period_key(utcnow(), "period")
    async with tenant_session(tid) as ts:
        await rate_period(ts, period_key=pk)          # our own invoice for this month

    async with get_platform_sessionmaker()() as session:
        await handle_event(
            session, _event("evt_inv_2", "invoice.paid", _paid_invoice_object())
        )
        await session.commit()

    async with tenant_session(tid) as ts:
        rows = await _invoices(ts)
    assert len(rows) == 2, "the provider invoice collided with our own for the same period"
    assert {r.period_key for r in rows} == {pk, "stripe:in_stripe_1"}


async def test_an_invoice_for_an_unknown_subscription_creates_nothing():
    """An event we cannot attribute must not mint an invoice against a guess."""
    from nexus.billing.webhooks import handle_event
    from nexus.core.db import get_platform_sessionmaker

    tid = await _tenant_with_stripe_sub()
    stray = {**_paid_invoice_object(), "id": "in_other", "subscription": "sub_nobody",
             "customer": "cus_nobody"}
    async with get_platform_sessionmaker()() as session:
        outcome = await handle_event(session, _event("evt_stray", "invoice.paid", stray))
        await session.commit()

    assert outcome.get("applied") is not True
    async with tenant_session(tid) as ts:
        assert await _invoices(ts) == []


async def test_a_failed_payment_is_not_recorded_as_paid():
    from nexus.billing.webhooks import handle_event
    from nexus.core.db import get_platform_sessionmaker

    tid = await _tenant_with_stripe_sub()
    failed = {**_paid_invoice_object(), "status": "open", "amount_paid": 0}
    async with get_platform_sessionmaker()() as session:
        await handle_event(session, _event("evt_fail", "invoice.payment_failed", failed))
        await session.commit()

    async with tenant_session(tid) as ts:
        rows = await _invoices(ts)
    assert rows and rows[0].status != "paid", "an unpaid invoice was recorded as paid"
