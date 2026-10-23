"""Bring the local copies of Stripe invoices made before 2026-10-23 up to what Stripe says.

Two things the webhook did not do until then, and that rows written before it still show:

* **A voided invoice stayed "finalized".** `invoice.voided` was not handled, so a declined plan
  switch left its invoice in the customer's list as unpaid for a switch that never happened.
* **No issue date, service period or lines.** The list showed `stripe:in_...` as the period.

Asks Stripe about each local copy that is still `finalized`, or has never been given those
fields, and takes Stripe's word: `void` and `paid` are final, anything else is left as it is.
Reports, then writes only when asked to. Nothing is priced here, and our own invoices (origin not
`provider`) are never touched: their status is ours.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class RepairReport:
    examined: int = 0
    voided: int = 0
    paid: int = 0
    unchanged: int = 0
    filled: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


async def repair_provider_invoices(provider, *, apply: bool, limit: int = 0) -> RepairReport:
    from sqlalchemy import select

    from nexus.billing.payments import PaymentError
    from nexus.billing.webhooks import _invoice_reading
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    report = RepairReport()
    async with get_platform_sessionmaker()() as session:
        rows = (await session.scalars(
            select(BillingInvoice).order_by(BillingInvoice.created_at))).all()
        for inv in rows:
            meta = dict(inv.meta or {})
            if meta.get("origin") != "provider":
                continue
            stripe_id = inv.psp_invoice_id or str(meta.get("psp_invoice_id") or "")
            needs_status = inv.status == "finalized"
            needs_reading = not meta.get("issued_at")
            if not stripe_id or not (needs_status or needs_reading):
                continue
            if limit and report.examined >= limit:
                break
            report.examined += 1
            try:
                obj = await provider.get_invoice(stripe_id)
            except PaymentError:
                report.failed += 1
                continue
            if needs_reading:
                meta.update(_invoice_reading(obj))
                report.filled += 1
            stripe_status = str(obj.get("status") or "")
            if needs_status and stripe_status == "void":
                inv.status = "void"
                meta["voided_at"] = meta.get("voided_at") or "repaired"
                report.voided += 1
            elif needs_status and stripe_status == "paid":
                inv.status = "paid"
                report.paid += 1
            elif needs_status:
                report.unchanged += 1
            inv.meta = meta
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return report
