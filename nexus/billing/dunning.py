# nexus/billing/dunning.py
"""Dunning: retry a failed collection on a schedule, then escalate.

Without this, a declined card is a silent write-off. The invoice sits `finalized` forever with
an error attached, nobody retries it, and nobody is told. Most failed card payments succeed on a
later attempt (the card is topped up, the temporary hold clears), so the retry schedule is where
most of the recoverable revenue actually is.

Three rules shape the design:

* **Never retry faster than the schedule.** Repeated declines damage the merchant's authorization
  rate with the card networks, and each attempt can cost a fee. The next attempt time is stored
  on the invoice, so a sweep that runs every minute still only charges on schedule.
* **Escalate, never silently void.** After the last attempt the subscription moves to
  ``past_due`` and the invoice stays ``finalized``. The debt remains real and visible; deciding
  to write it off is a human decision, not a background job's.
* **Idempotent at the money boundary.** Collection is keyed by invoice id at the provider, so a
  duplicated sweep cannot double-charge.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingInvoice, BillingSubscription

logger = logging.getLogger("nexus.billing.dunning")

# Days after the previous attempt before the next one. Config, not constants: recovery rates
# differ by customer base, and finance should be able to tune this without a deploy.
DEFAULT_SCHEDULE_DAYS = (1, 3, 7)


def _schedule() -> tuple[int, ...]:
    from nexus.core.config import get_settings

    raw = (getattr(get_settings(), "billing_dunning_schedule_days", "") or "").strip()
    if not raw:
        return DEFAULT_SCHEDULE_DAYS
    try:
        days = tuple(int(p.strip()) for p in raw.split(",") if p.strip())
        return days or DEFAULT_SCHEDULE_DAYS
    except ValueError:
        logger.warning("invalid dunning schedule %r; using the default", raw)
        return DEFAULT_SCHEDULE_DAYS


def is_due(invoice: BillingInvoice, *, now=None) -> bool:
    """True when this invoice is owed money and its next attempt time has arrived."""
    if invoice.status != "finalized" or invoice.total_cents <= 0:
        return False
    meta = invoice.meta or {}
    # Only invoices that have actually been attempted and failed enter dunning. An invoice that
    # has never been collected is the collection job's business, not dunning's.
    if not meta.get("payment_failed_at") and not meta.get("dunning_attempts"):
        return False
    if int(meta.get("dunning_attempts", 0)) >= len(_schedule()):
        return False

    now = now or utcnow()
    next_at = meta.get("next_attempt_at")
    if not next_at:
        return True
    try:
        from datetime import datetime

        return ensure_aware(datetime.fromisoformat(str(next_at))) <= now
    except ValueError:
        return True


async def attempt(ts: TenantSession, invoice: BillingInvoice, *, now=None) -> dict:
    """Make one dunning attempt against an invoice. Returns a summary."""
    from nexus.billing.collection import CollectionError, collect_invoice

    now = now or utcnow()
    meta = dict(invoice.meta or {})
    attempts = int(meta.get("dunning_attempts", 0))
    schedule = _schedule()

    try:
        result = await collect_invoice(ts, invoice.id)
    except CollectionError as exc:
        # Not collectable (no billing contact, wrong state). Not a decline — do not burn an
        # attempt on it, or a misconfigured customer would exhaust the schedule doing nothing.
        logger.warning("dunning skipped invoice %s: %s", invoice.id, exc)
        return {"invoice_id": invoice.id, "skipped": str(exc), "recovered": False}

    attempts += 1
    meta = dict(invoice.meta or {})
    meta["dunning_attempts"] = attempts
    meta["last_attempt_at"] = now.isoformat()

    if result.get("ok") and result.get("status") == "paid":
        meta.pop("next_attempt_at", None)
        meta["recovered_at"] = now.isoformat()
        invoice.meta = meta
        await ts.flush()
        logger.info("dunning recovered invoice %s on attempt %d", invoice.id, attempts)
        return {"invoice_id": invoice.id, "recovered": True, "attempts": attempts}

    if attempts >= len(schedule):
        # Out of attempts. Escalate rather than void: the debt is real.
        meta["dunning_exhausted_at"] = now.isoformat()
        meta.pop("next_attempt_at", None)
        invoice.meta = meta
        sub = await ts.first(BillingSubscription)
        if sub is not None and sub.status == "active":
            sub.status = "past_due"
        await ts.flush()
        logger.warning("dunning exhausted for invoice %s after %d attempts",
                       invoice.id, attempts)
        return {"invoice_id": invoice.id, "recovered": False, "attempts": attempts,
                "exhausted": True}

    meta["next_attempt_at"] = (now + timedelta(days=schedule[attempts])).isoformat()
    invoice.meta = meta
    await ts.flush()
    return {"invoice_id": invoice.id, "recovered": False, "attempts": attempts,
            "next_attempt_at": meta["next_attempt_at"]}


async def run_dunning(ts: TenantSession, *, now=None) -> dict:
    """Process every due invoice for one tenant."""
    now = now or utcnow()
    invoices = await ts.list(BillingInvoice, BillingInvoice.status == "finalized")
    due = [i for i in invoices if is_due(i, now=now)]

    recovered = exhausted = attempted = 0
    for invoice in due:
        try:
            outcome = await attempt(ts, invoice, now=now)
        except Exception:
            # One bad invoice must not stop the sweep for the rest of the tenant.
            logger.warning("dunning attempt failed for %s", invoice.id, exc_info=True)
            continue
        if outcome.get("skipped"):
            continue
        attempted += 1
        recovered += 1 if outcome.get("recovered") else 0
        exhausted += 1 if outcome.get("exhausted") else 0

    return {"due": len(due), "attempted": attempted,
            "recovered": recovered, "exhausted": exhausted}
