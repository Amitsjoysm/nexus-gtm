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
    BillingCapability,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPlan,
    BillingPlanEntitlement,
    BillingProrationAdjustment,
    BillingRateCard,
    BillingSubscription,
    BillingUsageRollup,
    proration_sort_key,
)

logger = logging.getLogger("nexus.billing.rating")

# What one credit costs when bought as OVERAGE, in cents.
#
# This was 1 cent, and that inverted the ladder. In-plan credits sell for 2.48c (Scale Annual) to
# 4.75c (Core), so overage at 1c made exceeding your plan **two to five times cheaper per credit
# than upgrading to cover the same usage**. A customer acting rationally would sit on the smallest
# plan and overflow forever, and the tier they were nominally on would stop meaning anything.
#
# 5c clears the dearest in-plan rate, so upgrading beats overflowing from every tier. The pressure
# is deliberately uneven: a Core customer at 4.75c feels a 5% premium, a Scale Annual customer at
# 2.48c feels 2x. That is the right way round — the customer on the cheapest rate has the most to
# gain from moving up, so should feel the most reason to.
#
# Margin at worst-case COGS ($0.004/credit) is 92%, so this is not priced to punish; it is priced
# to keep the ladder monotonic in the one place a customer can step outside it.
#
# A plan may still override per capability via `overage_price_credits`, which is how a negotiated
# enterprise rate avoids forking the catalog.
CREDIT_CENTS = 5

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


async def _credits_burned(ts: TenantSession, capability_id: str, period: str) -> float:
    """Credits already spent on this capability's overage during this period.

    Burns are negative deltas, so the sum is negated to give a positive "already paid" figure.
    """
    from sqlalchemy import func

    from nexus.models.billing import BillingCreditLedger

    total = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingCreditLedger.delta), 0)).where(
            BillingCreditLedger.tenant_id == ts.tenant_id,
            BillingCreditLedger.capability_id == capability_id,
            BillingCreditLedger.period_key == period,
            BillingCreditLedger.kind == "burn",
        )
    )
    return abs(float(total or 0))


async def rate_period(ts: TenantSession, *, period_key: str) -> BillingInvoice:
    """Rate one billing period into a draft invoice. Idempotent (upserts by period).

    A finalized invoice is returned untouched: history is never silently rewritten.
    """
    invoice = await ts.first(BillingInvoice, BillingInvoice.period_key == period_key)
    if invoice is not None and invoice.status in ("finalized", "paid", "void"):
        return invoice

    # Deterministic selection. The invariant is one active subscription per tenant, but rating
    # must be replayable even when that invariant is briefly violated (a mid-flight plan change,
    # a bad admin write): unordered "first active row" would rate the same period differently on
    # a re-run, which is exactly the guarantee this module exists to provide. Newest wins.
    subs = await ts.session.scalars(
        ts.select(
            BillingSubscription,
            BillingSubscription.status.in_(("trialing", "active", "past_due")),
        ).order_by(BillingSubscription.created_at.desc(), BillingSubscription.id.desc())
    )
    sub = subs.first()
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
        # Which capabilities measure a LEVEL rather than an action. Only these can appear on a
        # usage invoice now — see the loop below.
        gauges = {
            c.id for c in (await ts.session.scalars(select(BillingCapability))).all()
            if c.meter_kind == "gauge"
        }

        for r in sorted(rollups, key=lambda x: x.capability_id):
            # CREDIT-PAID USAGE IS NOT INVOICED AGAIN.
            #
            # Under credits-only billing the in-flight burn IS the charge: every metered request
            # deducts `credits_per_unit x quantity` from a balance the subscription already bought.
            # This loop was written for the previous model, where credits were burned only PAST the
            # quota and the remainder was invoiced — so leaving it alone became a double charge the
            # moment quotas stopped gating non-gauge capabilities: usage routinely exceeds the
            # quota number while every one of those units has already been paid for in credits.
            #
            # Gauges are the exception on the other side. `seat.member` and `platform.storage` are
            # never charged in credits (there is no request to price — they resolve to a live
            # count), so exceeding one is the only thing left that a usage invoice can legitimately
            # bill for. Skipping them too would make going over a seat cap free.
            if r.capability_id not in gauges:
                continue
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

            # Credits are pre-paid. Whatever this period's overage already burned must not be
            # charged again here, or the customer pays twice for the same units: once from the
            # balance at the moment of use, and once on the invoice.
            already_paid = await _credits_burned(ts, r.capability_id, period_key)
            if already_paid > 0:
                amount = max(0, amount - int(round(already_paid * CREDIT_CENTS)))

            if amount <= 0:
                continue
            lines.append(BillingInvoiceLine(
                invoice_id=invoice.id, kind="overage", capability_id=r.capability_id,
                description=f"{r.capability_id} overage ({over:g} over {quota})",
                quantity=over, unit_credits=unit_credits, amount_cents=amount,
            ))

    # 3. Mid-cycle plan changes. Read, never consumed: rating rebuilds lines from scratch, so a
    # row that were marked "applied" would vanish from the second pass and the invoice would
    # silently change. Both the credit and the charge are shown — an invoice carrying only the
    # charge reads as a second full month.
    for adj in sorted(
        await ts.list(
            BillingProrationAdjustment, BillingProrationAdjustment.period_key == period_key
        ),
        key=proration_sort_key,
    ):
        if not adj.amount_cents:
            continue
        lines.append(BillingInvoiceLine(
            invoice_id=invoice.id, kind="proration", description=adj.description,
            quantity=1, unit_credits=0, amount_cents=adj.amount_cents,
        ))

    for ln in lines:
        ts.add(ln)
    await ts.flush()

    subtotal = sum(ln.amount_cents for ln in lines)
    invoice.subtotal_cents = subtotal
    # A net credit is real, but it must never reach the payment provider as a negative charge.
    # The subtotal keeps the true arithmetic; the total is what we would collect, and you cannot
    # collect less than nothing. The remainder stays visible as the gap between the two.
    invoice.total_cents = max(0, subtotal)
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
