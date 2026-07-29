# nexus/billing/collection.py
"""Collect a finalized invoice through the payment provider.

This is the last link: rating produces an invoice, finalization freezes it, and collection
turns it into money. Everything before this point is arithmetic; this is the only place the
platform actually charges someone.

Two deliberate constraints:

* **Only finalized invoices are collectable.** A draft is still being recomputed; charging one
  would bill a number that can still change.
* **The idempotency key is the invoice id.** Retrying a collection — a timeout, a queue
  redelivery, an admin clicking twice — reaches the same key at the provider and returns the
  original charge instead of taking the money again.
"""
from __future__ import annotations

import logging

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingInvoice

logger = logging.getLogger("nexus.billing.collection")

COLLECTABLE_STATUSES = ("finalized",)


class CollectionError(RuntimeError):
    """Collection could not be attempted (wrong state, missing invoice, no billing contact)."""


async def collect_invoice(
    ts: TenantSession, invoice_id: str, *, email: str = "", name: str = ""
) -> dict:
    """Charge a finalized invoice. Idempotent on the invoice id.

    Returns a summary dict. A zero-total invoice is marked paid without touching the provider —
    there is nothing to collect, and a $0 charge is an error at every PSP.
    """
    from nexus.billing.payments import get_payment_provider
    from nexus.models.billing import BillingSubscription

    inv = await ts.get(BillingInvoice, invoice_id)
    if inv is None:
        raise CollectionError(f"invoice {invoice_id} not found")
    if inv.status == "paid":
        return {"invoice_id": inv.id, "status": "paid", "already": True,
                "reference": (inv.meta or {}).get("psp_reference", "")}
    if inv.status not in COLLECTABLE_STATUSES:
        raise CollectionError(
            f"invoice {invoice_id} is {inv.status}; only {COLLECTABLE_STATUSES} can be collected"
        )

    if inv.total_cents <= 0:
        inv.status = "paid"
        inv.meta = {**(inv.meta or {}), "collected_at": utcnow().isoformat(),
                    "psp_reference": "", "zero_total": True}
        await ts.flush()
        return {"invoice_id": inv.id, "status": "paid", "amount_cents": 0, "reference": ""}

    provider = get_payment_provider()
    sub = await ts.first(BillingSubscription)

    customer_id = sub.psp_customer_id if sub is not None else None
    if not customer_id:
        if not email:
            raise CollectionError(
                "no billing contact for this workspace; cannot create a payment customer"
            )
        customer_id = await provider.ensure_customer(
            tenant_id=ts.tenant_id, email=email, name=name
        )
        if sub is not None:
            sub.psp_customer_id = customer_id
            await ts.flush()

    result = await provider.charge(
        customer_id=customer_id,
        amount_cents=inv.total_cents,
        currency=inv.currency,
        # The invoice id IS the key: a retry can never collect twice.
        idempotency_key=f"invoice:{inv.id}",
        description=f"{inv.number or inv.period_key} - {inv.period_key}",
    )

    if result.ok:
        inv.status = "paid"
        inv.meta = {
            **(inv.meta or {}),
            "psp": result.provider,
            "psp_reference": result.reference,
            "collected_at": utcnow().isoformat(),
        }
        await ts.flush()
    else:
        # Left finalized, not marked failed: the money state is "not collected yet", and dunning
        # (out of scope here) decides what happens next.
        logger.warning("collection failed for invoice %s: %s", inv.id, result.detail)

    return {
        "invoice_id": inv.id,
        "status": inv.status,
        "amount_cents": inv.total_cents,
        "currency": inv.currency,
        "reference": result.reference,
        "provider": result.provider,
        "ok": result.ok,
    }
