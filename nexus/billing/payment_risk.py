# nexus/billing/payment_risk.py
"""Refunds, chargebacks and fraud warnings, applied to the workspace they concern (2026-10-12).

The webhook's refund and dispute branches looked the event's OWN id up as a PaymentIntent. A
refund event's object is a charge (`ch_...`) and a dispute's is a dispute (`dp_...`), so nothing
ever matched and every refund and chargeback was recorded nowhere. Here the payment is found by
the PaymentIntent or invoice the object names, and, for a payment we never recorded, by asking
Stripe about its charge.

What each event does, and deliberately does not do:

- **Refund**: recorded on the invoice. Refunding is an operator's decision, made in Stripe, and so
  is what the customer keeps: a goodwill refund taking credits back would undo the goodwill.
- **Dispute opened / early fraud warning**: the credits that payment bought are HELD, capped at
  what is unspent. Stripe debits a disputed amount from the balance at once, so the money has
  already left; credits it paid for should not keep being spent while the bank decides. Keyed by
  the CHARGE, so a fraud warning followed by a dispute on the same payment holds once.
- **Dispute won**: the hold is released. **Lost**: it stays, and the workspace records it.
- Nothing here cancels a subscription or refunds money. Both are visible, reversible decisions an
  operator makes with the customer's history in front of them; an automated one would be wrong for
  the customer whose card was flagged by mistake and could not be taken back.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.billing.payment_risk")

RISK_EVENTS = (
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.closed",
    "radar.early_fraud_warning.created",
)

#: Ledger rows for a hold carry this in `capability_id`, so the customer's credit report shows
#: "held for a disputed payment" rather than an unexplained deduction.
HOLD_CAPABILITY = "billing.dispute_hold"


def _str(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def references(event_type: str, obj: dict) -> tuple[str, str, str, str]:
    """(payment_intent, invoice, charge, customer) the event's object names."""
    if event_type == "charge.refunded":
        return (_str(obj.get("payment_intent")), _str(obj.get("invoice")), _str(obj.get("id")),
                _str(obj.get("customer")))
    return (_str(obj.get("payment_intent")), "", _str(obj.get("charge")),
            _str(obj.get("customer")))


async def _charge(charge_id: str) -> dict:
    """The charge as Stripe has it, or {}. Never raises: a lookup that fails leaves the event
    recorded as unmatched, which is what it was before."""
    if not charge_id:
        return {}
    try:
        from nexus.billing.payments import resolve_payment_provider

        return await (await resolve_payment_provider()).get_charge(charge_id) or {}
    except Exception:
        logger.warning("could not read charge %s from the payment provider", charge_id,
                       exc_info=True)
        return {}


async def locate(session, event_type: str, obj: dict):
    """The invoice (or None) and the subscription row a risk event concerns."""
    from sqlalchemy import select

    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.models.billing import BillingSubscription

    payment_intent, invoice_id, charge_id, customer = references(event_type, obj)
    invoice = await find_invoice_by_provider_reference(
        session, payment_intent=payment_intent, psp_invoice_id=invoice_id)
    if invoice is None and charge_id:
        found = await _charge(charge_id)
        invoice_id = invoice_id or _str(found.get("invoice"))
        payment_intent = payment_intent or _str(found.get("payment_intent"))
        customer = customer or _str(found.get("customer"))
        invoice = await find_invoice_by_provider_reference(
            session, payment_intent=payment_intent, psp_invoice_id=invoice_id)

    where = None
    if invoice is not None:
        where = BillingSubscription.tenant_id == invoice.tenant_id
    elif customer:
        where = BillingSubscription.psp_customer_id == customer
    sub = None
    if where is not None:
        sub = (await session.scalars(
            select(BillingSubscription).where(where)
            .order_by(BillingSubscription.created_at.desc()))).first()
    return invoice, sub, charge_id


async def _plan_credits(session, invoice, sub) -> float:
    from nexus.models.billing import BillingPlan

    plan_id = (getattr(invoice, "plan_id", None) or getattr(sub, "plan_id", None) or "")
    plan = await session.get(BillingPlan, plan_id) if plan_id else None
    return float(getattr(plan, "included_credits", 0) or 0)


async def _hold(ts, *, amount: float, charge_id: str, reason: str) -> float:
    """Hold up to ``amount`` credits, never below zero. Returns what was held."""
    from nexus.billing.credits import _append, _lock_tenant_credits, balance

    await _lock_tenant_credits(ts)
    held = max(0.0, min(float(amount), await balance(ts)))
    # Written even at zero: the row is also the marker that this payment's hold happened, which
    # is what a later release reads.
    await _append(ts, -held, kind="adjustment", reason=reason,
                  idempotency_key=f"payment_hold:{charge_id}", capability_id=HOLD_CAPABILITY)
    return held


async def _release(ts, *, charge_id: str, reason: str) -> float:
    from nexus.billing.credits import _append
    from nexus.models.billing import BillingCreditLedger

    hold = await ts.first(BillingCreditLedger,
                          BillingCreditLedger.idempotency_key == f"payment_hold:{charge_id}")
    amount = -float(hold.delta) if hold is not None else 0.0
    if amount <= 0:
        return 0.0
    applied = await _append(ts, amount, kind="adjustment", reason=reason,
                            idempotency_key=f"payment_release:{charge_id}",
                            capability_id=HOLD_CAPABILITY)
    return amount if applied else 0.0


def _listed(meta: dict, key: str, value: str, *, add: bool) -> None:
    items = [v for v in (meta.get(key) or []) if v != value]
    if add:
        items.append(value)
    meta[key] = items[-20:]


def _count(outcome: str) -> None:
    try:
        from nexus.core import metrics

        metrics.record_webhook_event("stripe", outcome)
    except Exception:  # a metric must never be why a webhook fails
        pass


async def apply(session, event_type: str, obj: dict, outcome: dict) -> dict:
    """Apply one risk event. The caller has verified and de-duplicated it."""
    from nexus.core.db import utcnow
    from nexus.core.tenancy import TenantSession, apply_rls

    invoice, sub, charge_id = await locate(session, event_type, obj)
    tenant_id = invoice.tenant_id if invoice is not None else getattr(sub, "tenant_id", "")
    if not tenant_id:
        outcome["note"] = "no matching invoice"
        return outcome
    await apply_rls(session, tenant_id)
    ts = TenantSession(session, tenant_id)
    outcome["tenant_id"] = tenant_id
    now = utcnow().isoformat()
    ref = _str(obj.get("id"))

    if invoice is not None:
        outcome["invoice_id"] = invoice.id
        meta = dict(invoice.meta or {})
        if not invoice.psp_reference and meta.get("psp_reference"):
            invoice.psp_reference = meta["psp_reference"]
        if not invoice.psp_invoice_id and meta.get("psp_invoice_id"):
            invoice.psp_invoice_id = meta["psp_invoice_id"]
        if event_type == "charge.refunded":
            meta["refunded_at"] = now
            meta["amount_refunded"] = obj.get("amount_refunded")
        elif event_type == "charge.dispute.created":
            meta["disputed_at"] = now
            meta["dispute_reason"] = str(obj.get("reason", ""))[:120]
            meta["dispute_status"] = str(obj.get("status", ""))[:40]
        elif event_type == "charge.dispute.closed":
            meta["dispute_status"] = str(obj.get("status", ""))[:40]
            meta["dispute_closed_at"] = now
        else:
            meta["fraud_warning_at"] = now
            meta["fraud_type"] = str(obj.get("fraud_type", ""))[:60]
        invoice.meta = meta

    sub_meta = dict(sub.meta or {}) if sub is not None else {}
    if event_type == "charge.refunded":
        _count("refund_recorded")
        outcome["note"] = "refund recorded"
    elif event_type in ("charge.dispute.created", "radar.early_fraud_warning.created"):
        dispute = event_type == "charge.dispute.created"
        reason = ("Held: a payment for this workspace was disputed" if dispute
                  else "Held: the card issuer reported a payment for this workspace as fraud")
        held = await _hold(ts, amount=await _plan_credits(session, invoice, sub),
                           charge_id=charge_id or ref, reason=reason)
        _listed(sub_meta, "open_disputes" if dispute else "fraud_warnings", ref, add=True)
        _count("dispute_opened" if dispute else "fraud_warning")
        logger.error("tenant %s: %s on charge %s; %.0f credits held", tenant_id,
                     "dispute opened" if dispute else "early fraud warning", charge_id, held)
        outcome["held"] = held
        outcome["note"] = "credits held"
    else:  # charge.dispute.closed
        status = str(obj.get("status") or "")
        _listed(sub_meta, "open_disputes", ref, add=False)
        if status == "won":
            outcome["released"] = await _release(ts, charge_id=charge_id or ref,
                                                 reason="Released: the dispute was won")
            _count("dispute_won")
            outcome["note"] = "dispute won; hold released"
        else:
            if status == "lost":
                _listed(sub_meta, "lost_disputes", ref, add=True)
            _count(f"dispute_{status or 'closed'}"[:40])
            logger.error("tenant %s: dispute %s closed as %s; the hold stays", tenant_id, ref,
                         status)
            outcome["note"] = f"dispute {status or 'closed'}; hold kept"
    if sub is not None:
        sub.meta = sub_meta
    await session.flush()
    outcome["applied"] = True
    return outcome
