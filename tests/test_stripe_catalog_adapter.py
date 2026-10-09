# tests/test_stripe_catalog_adapter.py
"""The Stripe calls behind Stripe-managed plans and subscriptions, checked at the wire."""
from __future__ import annotations

import httpx
import pytest

from nexus.billing.payments import (
    CREDITS_META_KEY,
    CREDITS_ON_PAYMENT,
    NoopPaymentProvider,
    PaymentError,
    StripePaymentProvider,
)


def _fake(monkeypatch, responder):
    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {}), idempotency_key))
        return responder(method, path, dict(form or {}))

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    return calls


def _error(status: int, text: str) -> PaymentError:
    err = PaymentError(text)
    err.status = status
    return err


async def test_the_http_status_rides_on_the_error():
    p = StripePaymentProvider("sk_test_x", max_retries=1)
    p._transport = httpx.MockTransport(
        lambda req: httpx.Response(404, json={"error": {"message": "No such price"}})
    )
    with pytest.raises(PaymentError) as exc:
        await p._get("/prices/price_gone")
    assert exc.value.status == 404


async def test_the_account_is_read_once_per_key(monkeypatch):
    from nexus.billing import payments

    payments._account_ids.clear()
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "acct_one"})
    assert await StripePaymentProvider("sk_test_acct1").account_id() == "acct_one"
    assert await StripePaymentProvider("sk_test_acct1").account_id() == "acct_one"
    assert [c[1] for c in calls] == ["/account"]


async def test_a_404_means_the_object_is_not_in_this_account(monkeypatch):
    def responder(m, p, f):
        raise _error(404, "stripe /prices/price_old -> 404: No such price")

    _fake(monkeypatch, responder)
    assert await StripePaymentProvider("sk_test_x").object_exists("prices", "price_old") is False


async def test_any_other_failure_is_not_read_as_missing(monkeypatch):
    def responder(m, p, f):
        raise _error(401, "stripe /prices/x -> 401: invalid key")

    _fake(monkeypatch, responder)
    with pytest.raises(PaymentError):
        await StripePaymentProvider("sk_test_x").object_exists("prices", "x")


async def test_a_deleted_customer_reads_as_missing(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "cus_1", "deleted": True})
    assert await StripePaymentProvider("sk_test_x").object_exists("customers", "cus_1") is False


async def test_a_known_product_gets_a_new_price_and_no_new_product(monkeypatch):
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "price_new"})
    refs = await StripePaymentProvider("sk_test_x").ensure_plan_price(
        plan_id="launch", name="Launch", amount_cents=9900, product_id="prod_keep",
    )
    assert refs == {"product_id": "prod_keep", "price_id": "price_new"}
    assert [c[1] for c in calls] == ["/prices"]
    assert calls[0][2]["product"] == "prod_keep"


async def test_archiving_and_renaming(monkeypatch):
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "x"})
    p = StripePaymentProvider("sk_test_x")
    await p.set_price_active(price_id="price_1", active=False)
    await p.rename_product(product_id="prod_1", name="Scale")
    assert calls[0][:3] == ("POST", "/prices/price_1", {"active": "false"})
    assert calls[1][:3] == ("POST", "/products/prod_1", {"name": "Scale"})


async def test_an_invoiced_subscription_is_created_finalized_and_sent(monkeypatch):
    def responder(m, p, f):
        if p == "/subscriptions":
            return {"id": "sub_1", "status": "active", "customer": "cus_1",
                    "latest_invoice": "in_1", "collection_method": "send_invoice",
                    "current_period_start": 1, "current_period_end": 2}
        if p == "/invoices/in_1/finalize":
            return {"id": "in_1", "status": "open"}
        if p == "/invoices/in_1/send":
            return {"id": "in_1", "status": "open",
                    "hosted_invoice_url": "https://invoice.stripe.com/i/1"}
        raise AssertionError(p)

    calls = _fake(monkeypatch, responder)
    out = await StripePaymentProvider("sk_test_x").create_invoiced_subscription(
        customer_id="cus_1", price_id="price_1", tenant_id="t1", plan_id="launch",
        days_until_due=14, idempotency_key="k1",
    )
    form = calls[0][2]
    assert form["collection_method"] == "send_invoice"
    assert form["days_until_due"] == "14"
    assert form["items[0][price]"] == "price_1"
    assert form["metadata[tenant_id]"] == "t1" and form["metadata[plan_id]"] == "launch"
    assert form[f"metadata[{CREDITS_META_KEY}]"] == CREDITS_ON_PAYMENT
    assert calls[0][3] == "k1"
    assert [c[1] for c in calls] == ["/subscriptions", "/invoices/in_1/finalize",
                                     "/invoices/in_1/send"]
    assert out["id"] == "sub_1"
    assert out["hosted_invoice_url"] == "https://invoice.stripe.com/i/1"
    assert out["collection_method"] == "send_invoice"


async def test_an_invoice_that_cannot_be_sent_does_not_lose_the_subscription(monkeypatch):
    def responder(m, p, f):
        if p == "/subscriptions":
            return {"id": "sub_2", "status": "active", "customer": "cus_1",
                    "latest_invoice": "in_2"}
        raise _error(400, "stripe /invoices/in_2/finalize -> 400: already finalized")

    _fake(monkeypatch, responder)
    out = await StripePaymentProvider("sk_test_x").create_invoiced_subscription(
        customer_id="cus_1", price_id="p", tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k2",
    )
    assert out["id"] == "sub_2"
    assert "already finalized" in out["invoice_note"]


async def test_a_price_change_moves_the_single_item_and_invoices_the_difference(monkeypatch):
    def responder(m, p, f):
        if m == "GET":
            return {"id": "sub_1", "items": {"data": [{"id": "si_1"}]}}
        return {"id": "sub_1", "status": "active", "collection_method": "charge_automatically"}

    calls = _fake(monkeypatch, responder)
    await StripePaymentProvider("sk_test_x").change_subscription_price(
        subscription_id="sub_1", price_id="price_2", plan_id="accelerate",
    )
    assert calls[1][:2] == ("POST", "/subscriptions/sub_1")
    assert calls[1][2] == {
        "items[0][id]": "si_1", "items[0][price]": "price_2",
        "proration_behavior": "always_invoice", "metadata[plan_id]": "accelerate",
    }


async def test_a_multi_item_subscription_is_refused(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "sub_1", "items": {"data": [{"id": "a"},
                                                                        {"id": "b"}]}})
    with pytest.raises(PaymentError):
        await StripePaymentProvider("sk_test_x").change_subscription_price(
            subscription_id="sub_1", price_id="p", plan_id="x",
        )


async def test_checkout_reports_when_its_link_expires(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "cs_1", "url": "https://checkout.stripe.com/c/1",
                                        "expires_at": 1999999999})
    out = await StripePaymentProvider("sk_test_x").create_checkout_session(
        tenant_id="t", plan_id="launch", price_id="price_1",
    )
    assert out["expires_at"] == 1999999999


async def test_the_offline_provider_supports_every_new_call():
    p = NoopPaymentProvider()
    assert await p.account_id() == "noop"
    assert await p.object_exists("prices", "anything") is True
    refs = await p.ensure_plan_price(plan_id="launch", name="Launch", amount_cents=9900,
                                     product_id="prod_x")
    assert refs["product_id"] == "prod_x"
    await p.set_price_active(price_id=refs["price_id"], active=False)
    assert p.price_active[refs["price_id"]] is False
    await p.rename_product(product_id="prod_x", name="Launch Plus")
    assert p.renames[-1] == {"product_id": "prod_x", "name": "Launch Plus"}
    sub = await p.create_invoiced_subscription(
        customer_id="c", price_id=refs["price_id"], tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k",
    )
    again = await p.create_invoiced_subscription(
        customer_id="c", price_id=refs["price_id"], tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k",
    )
    assert sub["id"] == again["id"] and sub["collection_method"] == "send_invoice"
    changed = await p.change_subscription_price(subscription_id=sub["id"], price_id="p2",
                                                plan_id="accelerate")
    assert changed["metadata"]["plan_id"] == "accelerate"
    assert changed["collection_method"] == "send_invoice"
    link = await p.create_checkout_session(tenant_id="t", plan_id="launch", price_id="p")
    assert link["expires_at"] > 0
