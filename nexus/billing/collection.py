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
from nexus.models.billing import BillingInvoice, BillingInvoiceLine

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
    from nexus.billing.payments import resolve_payment_provider
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

    provider = await resolve_payment_provider()
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

    # Raise a real invoice at the provider, carrying the lines we rated, rather than a bare charge.
    #
    # A charge collects money and leaves the customer nothing to look at: no hosted page, no PDF,
    # no line items, and nothing their finance team can file. That was fine while subscriptions
    # were the only thing Stripe saw, but usage and overage are billed here — so the charges people
    # most want an explanation for were exactly the ones with no document behind them.
    #
    # Our rating stays the source of truth. We send the lines; the provider does not price
    # anything. Letting it compute the total would put the arithmetic somewhere `reconcile.py`
    # cannot check.
    lines = [
        {"description": ln.description or (ln.capability_id or ln.kind),
         "amount_cents": int(ln.amount_cents)}
        for ln in await ts.list(BillingInvoiceLine, BillingInvoiceLine.invoice_id == inv.id)
    ]
    if not lines:
        # A rated invoice with a total and no lines should not exist, but if it does, bill the
        # total rather than raising an empty invoice — the customer owes what the invoice says.
        lines = [{"description": f"Usage for {inv.period_key}",
                  "amount_cents": int(inv.total_cents)}]

    try:
        raised = await provider.create_invoice(
            customer_id=customer_id,
            lines=lines,
            currency=inv.currency,
            # Our invoice id IS the key: a retry can never bill twice, and it ties the two sides
            # together for reconciliation without guessing.
            idempotency_key=inv.id,
            description=f"{inv.number or inv.period_key} — usage for {inv.period_key}",
        )
    except Exception as exc:
        logger.warning("collection failed for invoice %s: %s", inv.id, exc)
        return {
            "invoice_id": inv.id, "status": inv.status, "amount_cents": inv.total_cents,
            "currency": inv.currency, "reference": "", "provider": provider.name, "ok": False,
        }

    paid = str(raised.get("status", "")) == "paid"
    # Mirrored onto the indexed column as well as into meta. The column is what a webhook looks
    # up (meta is JSON and unindexable); meta stays because the reconciler reads it and because a
    # row written by the previous release only has the meta copy.
    inv.psp_invoice_id = raised.get("id", "") or None
    inv.meta = {
        **(inv.meta or {}),
        "psp": provider.name,
        "psp_invoice_id": raised.get("id", ""),
        # Stored so the customer's own Billing page can link to the document. Empty for the noop
        # provider, and the UI hides the link rather than offering a URL that goes nowhere.
        "hosted_invoice_url": raised.get("hosted_url", ""),
        "invoice_pdf_url": raised.get("pdf_url", ""),
    }
    if paid:
        inv.status = "paid"
        inv.psp_reference = raised.get("id", "") or None
        inv.meta = {**inv.meta, "psp_reference": raised.get("id", ""),
                    "collected_at": utcnow().isoformat()}
    else:
        # Left finalized, not marked failed: the money state is "not collected yet". The invoice
        # exists and is payable from its hosted page, and dunning decides what happens next.
        logger.info("invoice %s raised at %s but not paid (status %s)",
                    inv.id, provider.name, raised.get("status"))
    await ts.flush()

    return {
        "invoice_id": inv.id,
        "status": inv.status,
        "amount_cents": inv.total_cents,
        "currency": inv.currency,
        "reference": str(raised.get("id", "")),
        "hosted_url": str(raised.get("hosted_url", "")),
        "provider": provider.name,
        "ok": paid,
    }
