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
        assert len(provider.charges) == 1
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
        assert len(provider.charges) == 1        # charged exactly once
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
