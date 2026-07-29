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
