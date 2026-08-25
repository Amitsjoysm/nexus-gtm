# tests/test_billing_payments.py
"""The payment seam: offline by default, inert without keys, never silently faking a charge."""
from __future__ import annotations

import pytest


def test_default_provider_is_offline():
    """Nothing in a default deployment can move money."""
    from nexus.billing.payments import NoopPaymentProvider, get_payment_provider, set_payment_provider
    from nexus.core.config import get_settings

    set_payment_provider(None)
    assert get_settings().payment_provider == "noop"
    assert isinstance(get_payment_provider(), NoopPaymentProvider)


async def test_noop_records_intent_without_moving_money():
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    cid = await p.ensure_customer(tenant_id="t1", email="a@b.com", name="Acme")
    res = await p.charge(customer_id=cid, amount_cents=7900, currency="USD",
                         idempotency_key="inv-1", description="Growth plan")

    assert res.ok is True
    assert res.provider == "noop"
    assert res.amount_cents == 7900
    assert len(p.charges) == 1                 # the intent is inspectable, not invisible


async def test_noop_charge_is_idempotent():
    """A retried collection must not charge twice, offline or not."""
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    cid = await p.ensure_customer(tenant_id="t1", email="a@b.com")
    first = await p.charge(customer_id=cid, amount_cents=500, currency="USD",
                           idempotency_key="same")
    second = await p.charge(customer_id=cid, amount_cents=500, currency="USD",
                            idempotency_key="same")

    assert first.reference == second.reference
    assert len(p.charges) == 1


async def test_noop_customer_id_is_stable():
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    a = await p.ensure_customer(tenant_id="t1", email="a@b.com")
    b = await p.ensure_customer(tenant_id="t1", email="a@b.com")
    assert a == b


async def test_unconfigured_stripe_refuses_rather_than_pretending():
    """The failure mode that matters: a selected-but-unkeyed provider must never return a
    success that no money backs."""
    from nexus.billing.payments import PaymentNotConfigured, StripePaymentProvider

    p = StripePaymentProvider("")
    assert p.configured is False
    with pytest.raises(PaymentNotConfigured):
        await p.ensure_customer(tenant_id="t1", email="a@b.com")
    with pytest.raises(PaymentNotConfigured):
        await p.charge(customer_id="cus_x", amount_cents=100, currency="USD",
                       idempotency_key="k")
    with pytest.raises(PaymentNotConfigured):
        await p.refund(reference="pi_x", amount_cents=100, idempotency_key="k")
    # Checkout and portal are the two self-serve surfaces; an unkeyed provider must not hand a
    # customer a URL that goes nowhere.
    with pytest.raises(PaymentNotConfigured):
        await p.create_checkout_session(
            tenant_id="t1", plan_id="growth", price_id="price_x",
            success_url="https://app/ok", cancel_url="https://app/no",
        )
    with pytest.raises(PaymentNotConfigured):
        await p.create_billing_portal_session(customer_id="cus_x", return_url="https://app")


def test_stripe_is_selected_only_when_configured(monkeypatch):
    from nexus.billing.payments import (
        NoopPaymentProvider,
        StripePaymentProvider,
        build_payment_provider_from_settings,
    )
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "payment_provider", "stripe")
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_x")
    built = build_payment_provider_from_settings()
    assert isinstance(built, StripePaymentProvider) and built.configured is True

    monkeypatch.setattr(s, "payment_provider", "noop")
    assert isinstance(build_payment_provider_from_settings(), NoopPaymentProvider)


def test_override_restores_from_settings():
    from nexus.billing.payments import (
        NoopPaymentProvider,
        get_payment_provider,
        set_payment_provider,
    )

    sentinel = NoopPaymentProvider()
    set_payment_provider(sentinel)
    assert get_payment_provider() is sentinel
    set_payment_provider(None)
    assert get_payment_provider() is not sentinel


# ---- collection: invoice -> money ----------------------------------------------------------

async def _finalized_invoice(plan_id: str = "growth"):
    """A tenant with one finalized invoice. Growth's base fee gives it a non-zero total."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingSubscription
    from tests.conftest import make_tenant, tenant_session

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
        await rebuild_rollups(ts)
        inv = await rate_period(ts, period_key=period_key(utcnow(), "period"))
        await finalize_invoice(ts, inv.id)
        return tid, inv.id


async def test_collecting_a_finalized_invoice_marks_it_paid():
    from nexus.billing.collection import collect_invoice
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _finalized_invoice()
        async with tenant_session(tid) as ts:
            res = await collect_invoice(ts, inv_id, email="ap@acme.test", name="Acme")
            assert res["ok"] is True
            assert res["amount_cents"] == 7900
            inv = await ts.get(BillingInvoice, inv_id)
            assert inv.status == "paid"
            assert inv.meta["psp_reference"]
            # The provider now receives a real invoice carrying the lines we rated, so the customer
            # gets a document rather than an unexplained card charge.
            assert inv.meta["psp_invoice_id"]
        assert len(provider.invoices) == 1
        assert provider.charges == []
        raised = provider.invoices[0]
        assert raised["amount_cents"] == 7900
        assert raised["lines"], "the rated lines travel to the provider, not just a total"
    finally:
        set_payment_provider(None)


async def test_collection_is_idempotent_on_the_invoice():
    """A retried collection must not take the money twice. The invoice id is the key."""
    from nexus.billing.collection import collect_invoice
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from tests.conftest import tenant_session

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _finalized_invoice()
        async with tenant_session(tid) as ts:
            first = await collect_invoice(ts, inv_id, email="ap@acme.test")
            second = await collect_invoice(ts, inv_id, email="ap@acme.test")
        assert first["reference"]
        assert second.get("already") is True
        # One invoice raised, not two. Collection now publishes a real invoice at the provider
        # rather than a bare charge, so this is where the "exactly once" property lives.
        assert len(provider.invoices) == 1
        assert provider.charges == []
    finally:
        set_payment_provider(None)


async def test_a_draft_invoice_cannot_be_collected():
    """A draft is still being recomputed; charging one would bill a number that can change."""
    import pytest as _pytest

    from nexus.billing.collection import CollectionError, collect_invoice
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from nexus.billing.rating import rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups
    from nexus.core.db import utcnow
    from tests.conftest import tenant_session

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        tid, _ = await _finalized_invoice()
        async with tenant_session(tid) as ts:
            await rebuild_rollups(ts)
            draft = await rate_period(ts, period_key=period_key(utcnow(), "period"))
            draft.status = "draft"
            await ts.flush()
            with _pytest.raises(CollectionError):
                await collect_invoice(ts, draft.id, email="ap@acme.test")
        assert provider.charges == []
    finally:
        set_payment_provider(None)


async def test_zero_total_invoice_never_touches_the_provider():
    """A $0 charge is an error at every PSP; there is nothing to collect."""
    from nexus.billing.collection import collect_invoice
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider
    from tests.conftest import tenant_session

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        tid, inv_id = await _finalized_invoice("free")       # base price 0
        async with tenant_session(tid) as ts:
            res = await collect_invoice(ts, inv_id, email="ap@acme.test")
            assert res["status"] == "paid"
            assert res["amount_cents"] == 0
        assert provider.charges == []
    finally:
        set_payment_provider(None)


async def test_noop_attach_records_the_card():
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    cid = await p.ensure_customer(tenant_id="t1", email="a@b.com")
    assert await p.attach_payment_method(customer_id=cid, payment_method_id="pm_x") is True
    assert p.payment_methods[cid] == "pm_x"


# ---- hosted checkout + portal (M12) ----------------------------------------------------------

async def test_noop_checkout_session_is_inspectable():
    """The offline provider has to be usable as a test double: what would have been sent to
    Stripe is recorded rather than lost."""
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    out = await p.create_checkout_session(
        tenant_id="t1", plan_id="growth", price_id="price_growth",
        customer_id="cus_1", success_url="https://app/ok", cancel_url="https://app/no",
    )

    assert out["id"] and out["url"]
    assert out["metadata"] == {"tenant_id": "t1", "plan_id": "growth"}
    assert len(p.checkout_sessions) == 1
    assert p.checkout_sessions[0]["price_id"] == "price_growth"


async def test_noop_checkout_sessions_are_distinct():
    """Each click mints a fresh session; reusing one would hand out an expired URL."""
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    a = await p.create_checkout_session(tenant_id="t1", plan_id="growth", price_id="pr")
    b = await p.create_checkout_session(tenant_id="t1", plan_id="growth", price_id="pr")
    assert a["id"] != b["id"]


async def test_noop_portal_session_records_the_customer():
    from nexus.billing.payments import NoopPaymentProvider

    p = NoopPaymentProvider()
    out = await p.create_billing_portal_session(
        customer_id="cus_1", return_url="https://app/settings"
    )
    assert out["id"] and out["url"]
    assert p.portal_sessions[0]["return_url"] == "https://app/settings"


async def test_stripe_checkout_posts_a_subscription_session(monkeypatch):
    """Shape check against the real adapter without a network: mode=subscription, the tenant
    stamped on BOTH the session and the subscription, and the price as a line item."""
    from nexus.billing.payments import StripePaymentProvider

    sent: dict = {}

    async def _fake_post(self, path, form, idempotency_key=""):
        sent["path"] = path
        sent["form"] = form
        sent["idempotency_key"] = idempotency_key
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/cs_test_1",
                "customer": "cus_1"}

    monkeypatch.setattr(StripePaymentProvider, "_post", _fake_post)
    p = StripePaymentProvider("sk_test_x")
    out = await p.create_checkout_session(
        tenant_id="t1", plan_id="growth", price_id="price_growth", customer_id="cus_1",
        success_url="https://app/ok", cancel_url="https://app/no",
    )

    assert out["id"] == "cs_test_1"
    assert out["url"].startswith("https://checkout.stripe.com/")
    assert sent["path"] == "/checkout/sessions"
    form = sent["form"]
    assert form["mode"] == "subscription"
    assert form["line_items[0][price]"] == "price_growth"
    assert form["metadata[tenant_id]"] == "t1"
    assert form["metadata[plan_id]"] == "growth"
    # Without this, every later customer.subscription.* event would need a customer lookup.
    assert form["subscription_data[metadata][tenant_id]"] == "t1"
    assert form["customer"] == "cus_1"
    # No idempotency key: replaying one would return an expired session URL.
    assert sent["idempotency_key"] == ""


async def test_stripe_portal_posts_the_customer(monkeypatch):
    from nexus.billing.payments import StripePaymentProvider

    sent: dict = {}

    async def _fake_post(self, path, form, idempotency_key=""):
        sent["path"] = path
        sent["form"] = form
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}

    monkeypatch.setattr(StripePaymentProvider, "_post", _fake_post)
    p = StripePaymentProvider("sk_test_x")
    out = await p.create_billing_portal_session(
        customer_id="cus_1", return_url="https://app/settings"
    )

    assert out["id"] == "bps_1" and out["url"].startswith("https://billing.stripe.com/")
    assert sent["path"] == "/billing_portal/sessions"
    assert sent["form"] == {"customer": "cus_1", "return_url": "https://app/settings"}


def test_every_provider_implements_the_whole_seam():
    """A method added to the ABC but not to one implementation is a crash at the worst possible
    moment — someone trying to pay."""
    from nexus.billing.payments import (
        NoopPaymentProvider,
        PaymentProvider,
        StripePaymentProvider,
    )

    required = {
        name for name in dir(PaymentProvider)
        if getattr(getattr(PaymentProvider, name), "__isabstractmethod__", False)
    }
    assert "create_checkout_session" in required
    assert "create_billing_portal_session" in required
    for impl in (NoopPaymentProvider, StripePaymentProvider):
        assert not getattr(impl, "__abstractmethods__", frozenset()), impl.__name__
