# nexus/billing/rating.py
"""Rating: turn a period's rollups into invoice lines.

Deterministic and replayable by construction — it reads only rollups + config, so re-rating a
period always reproduces identical lines (docs/billing/04-Pricing-Engine.md §2). That property is
what makes an invoice defensible in a dispute.

Money is integer cents everywhere. Credits are the intermediate unit (1 credit = $0.01).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    BillingPlan,
    BillingPlanEntitlement,
    BillingRateCard,
    BillingSubscription,
    BillingUsageRollup,
)

logger = logging.getLogger("nexus.billing.rating")

CREDIT_CENTS = 1  # 1 credit = $0.01 = 1 cent

# Plan classes that are never charged usage overage.
_NO_OVERAGE_CLASSES = {"unlimited", "internal", "partner"}


def tiered_credits(units: float, card: BillingRateCard) -> float:
    """Credits for ``units`` under the card's volume ladder (flat rate when no tiers)."""
    tiers = list(card.tiers or [])
    if not tiers:
        return float(units) * float(card.credits_per_unit)
    total = 0.0
    remaining = float(units)
    consumed = 0.0
    for tier in tiers:
        upto = tier.get("upto")
        price = float(tier.get("credits", card.credits_per_unit))
        if upto is None:
            total += remaining * price
            remaining = 0.0
            break
        span = max(0.0, float(upto) - consumed)
        take = min(remaining, span)
        total += take * price
        remaining -= take
        consumed += take
        if remaining <= 0:
            break
    if remaining > 0:  # ladder didn't cover everything; charge the base rate for the tail
        total += remaining * float(card.credits_per_unit)
    return total


async def rate_period(ts: TenantSession, *, period_key: str) -> BillingInvoice:
    """Rate one billing period into a draft invoice. Idempotent (upserts by period).

    A finalized invoice is returned untouched: history is never silently rewritten.
    """
    invoice = await ts.first(BillingInvoice, BillingInvoice.period_key == period_key)
    if invoice is not None and invoice.status in ("finalized", "paid", "void"):
        return invoice

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    if invoice is None:
        invoice = BillingInvoice(period_key=period_key, status="draft",
                                 plan_id=sub.plan_id if sub else None)
        ts.add(invoice)
        await ts.flush()
    else:
        # Rebuild lines from scratch so re-rating is a pure function of current data.
        for old in await ts.list(
            BillingInvoiceLine, BillingInvoiceLine.invoice_id == invoice.id
        ):
            await ts.delete(old)
        await ts.flush()

    lines: list[BillingInvoiceLine] = []

    # 1. Base subscription fee.
    if plan is not None and plan.base_price_cents:
        lines.append(BillingInvoiceLine(
            invoice_id=invoice.id, kind="base", description=f"{plan.name} plan",
            quantity=1, unit_credits=0, amount_cents=plan.base_price_cents,
        ))

    # 2. Usage overage, per capability.
    charge_overage = plan is not None and plan.plan_class not in _NO_OVERAGE_CLASSES
    if charge_overage and sub is not None:
        ents = {
            e.capability_id: e
            for e in (
                await ts.session.scalars(
                    select(BillingPlanEntitlement).where(
                        BillingPlanEntitlement.plan_id == sub.plan_id
                    )
                )
            ).all()
        }
        cards = {
            c.capability_id: c
            for c in (await ts.session.scalars(select(BillingRateCard))).all()
        }
        rollups = await ts.list(
            BillingUsageRollup,
            BillingUsageRollup.period_kind == "period",
            BillingUsageRollup.period_key == period_key,
        )
        for r in sorted(rollups, key=lambda x: x.capability_id):
            ent = ents.get(r.capability_id)
            quota = ent.quota if ent is not None else None
            if quota is None:
                continue                      # unlimited/unpriced -> nothing to charge
            over = float(r.quantity) - float(quota)
            if over <= 0:
                continue
            # A plan may set its own overage price. It overrides the global rate card, so a
            # negotiated enterprise rate never requires forking the catalog.
            if ent.overage_price_credits is not None:
                unit_credits = float(ent.overage_price_credits)
                credits = over * unit_credits
            else:
                card = cards.get(r.capability_id)
                if card is None or not card.active:
                    continue
                credits = tiered_credits(over, card)
                unit_credits = float(card.credits_per_unit)
            amount = int(round(credits * CREDIT_CENTS))
            if amount <= 0:
                continue
            lines.append(BillingInvoiceLine(
                invoice_id=invoice.id, kind="overage", capability_id=r.capability_id,
                description=f"{r.capability_id} overage ({over:g} over {quota})",
                quantity=over, unit_credits=unit_credits, amount_cents=amount,
            ))

    for ln in lines:
        ts.add(ln)
    await ts.flush()

    subtotal = sum(ln.amount_cents for ln in lines)
    invoice.subtotal_cents = subtotal
    invoice.total_cents = subtotal
    invoice.currency = plan.currency if plan else "USD"
    await ts.flush()
    return invoice


async def finalize_invoice(ts: TenantSession, invoice_id: str) -> BillingInvoice:
    """Freeze an invoice and assign its number. Idempotent."""
    inv = await ts.get(BillingInvoice, invoice_id)
    if inv is None:
        raise ValueError(f"invoice {invoice_id} not found")
    if inv.status != "draft":
        return inv
    now = utcnow()
    inv.status = "finalized"
    inv.finalized_at = now
    inv.number = f"INV-{now:%Y%m}-{inv.id[:8].upper()}"
    await ts.flush()
    return inv
