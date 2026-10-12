"""An invoice Stripe raised is collected by Stripe, never also by our dunning (2026-10-12).

Found auditing the payment paths for production. A Stripe subscription renewal that failed twice
went: `invoice.payment_failed` recorded a local copy of Stripe's invoice (`finalized`, origin
`provider`); the retry's `invoice.payment_failed` matched that copy and stamped `payment_failed_at`;
and `payment_failed_at` is exactly what puts an invoice into OUR dunning. Dunning then called
`collect_invoice`, which raised a NEW Stripe invoice for the same amount and charged the card,
while Stripe's own retries went on collecting the original. A card that recovered paid twice.
"""
from __future__ import annotations

from tests.test_billing_checkout_api import _seeded_tenant, noop_provider  # noqa: F401
from tests.test_one_stripe_subscription import _follow
from tests.test_stripe_invoice_credits import _send


def _failed(tid: str) -> dict:
    return {"id": "in_fail1", "object": "invoice", "subscription": "sub_f1",
            "customer": f"cus_{tid[:6]}", "payment_intent": "pi_fail1", "amount_due": 9900,
            "amount_paid": 0, "currency": "usd", "status": "open",
            "metadata": {"tenant_id": tid}}


async def test_dunning_never_collects_an_invoice_stripe_raised(client, monkeypatch, noop_provider):
    from nexus.billing.dunning import run_dunning
    from tests.conftest import tenant_session

    _, tid = await _seeded_tenant(client, slug="sc1")
    await _follow(tid, "sub_f1")
    await _send(client, monkeypatch, "invoice.payment_failed", _failed(tid))
    await _send(client, monkeypatch, "invoice.payment_failed", _failed(tid))   # Stripe's retry

    async with tenant_session(tid) as ts:
        result = await run_dunning(ts)
    assert result["attempted"] == 0
    assert noop_provider.invoices == [] and noop_provider.charges == []


async def test_collect_invoice_refuses_an_invoice_stripe_raised(client, monkeypatch, noop_provider):
    import pytest

    from nexus.billing.collection import CollectionError, collect_invoice
    from nexus.models.billing import BillingInvoice
    from tests.conftest import tenant_session

    _, tid = await _seeded_tenant(client, slug="sc2")
    await _follow(tid, "sub_f1")
    await _send(client, monkeypatch, "invoice.payment_failed", _failed(tid))
    async with tenant_session(tid) as ts:
        inv = await ts.first(BillingInvoice, BillingInvoice.period_key == "stripe:in_fail1")
        with pytest.raises(CollectionError, match="Stripe collects"):
            await collect_invoice(ts, inv.id)
    assert noop_provider.invoices == []
