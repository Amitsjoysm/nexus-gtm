# nexus/api/routers/billing.py
"""Tenant-facing billing surface: what plan am I on, and what have I used?

Read-only in this milestone. Powers the in-app usage meters and the upgrade prompts that a 402
deep-links into (docs/billing/10-Usage-Tracking.md §2).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
    BillingUsageRollup,
)

router = APIRouter(prefix="/billing", tags=["billing"])


class CapabilityUsageOut(BaseModel):
    capability_id: str
    name: str
    category: str
    unit: str
    used: float
    quota: int | None = None
    mode: str


class UsageOut(BaseModel):
    plan: str | None
    plan_name: str | None
    period: str
    capabilities: list[CapabilityUsageOut]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    ts: TenantSession = Depends(get_tenant_session),
    # Deliberately rep+ (manage_accounts): this returns quota counts, not money, and the rep
    # hitting a 402 is exactly who needs to see "17 of 20 used". Money surfaces are admin-only.
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> UsageOut:
    from sqlalchemy import func

    from nexus.billing.rollups import period_key, period_start
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    now = utcnow()
    key = period_key(now, "period")

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    ents: dict[str, BillingPlanEntitlement] = {}
    if sub is not None:
        for e in (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id
                )
            )
        ).all():
            ents[e.capability_id] = e

    rollups = {
        r.capability_id: float(r.quantity)
        for r in (
            await ts.session.scalars(
                ts.select(
                    BillingUsageRollup,
                    BillingUsageRollup.period_kind == "period",
                    BillingUsageRollup.period_key == key,
                )
            )
        ).all()
    }

    # The rollup alone lags reality between sweeps. Enforcement counts rollup + unrolled tail,
    # so this must too, or a rep sees "17 of 20" while a 402 tells them they are at 20. One
    # grouped query keeps that agreement without a per-capability N+1.
    tail = {
        cap: float(qty or 0)
        for cap, qty in (
            await ts.session.execute(
                select(
                    BillingUsageEvent.capability_id,
                    func.sum(BillingUsageEvent.quantity),
                )
                .where(
                    BillingUsageEvent.tenant_id == ts.tenant_id,
                    BillingUsageEvent.rolled_at.is_(None),
                    BillingUsageEvent.occurred_at >= period_start(now),
                )
                .group_by(BillingUsageEvent.capability_id)
            )
        ).all()
    }

    caps = (
        await ts.session.scalars(
            select(BillingCapability)
            .where(BillingCapability.active == True)  # noqa: E712
            .order_by(BillingCapability.category, BillingCapability.id)
        )
    ).all()

    out = []
    for c in caps:
        ent = ents.get(c.id)
        used = rollups.get(c.id, 0.0) + tail.get(c.id, 0.0)
        # Only surface things the customer can actually reason about: anything they've used, or
        # anything their plan puts a number on. Pure-internal shadow meters stay hidden.
        if used == 0 and ent is None:
            continue
        out.append(
            CapabilityUsageOut(
                capability_id=c.id, name=c.name, category=c.category, unit=c.unit,
                used=used, quota=ent.quota if ent else None,
                mode=ent.mode if ent else c.default_mode,
            )
        )
    return UsageOut(
        plan=sub.plan_id if sub else None,
        plan_name=plan.name if plan else None,
        period=key,
        capabilities=out,
    )


class CreditEntryOut(BaseModel):
    id: str
    delta: float
    kind: str
    reason: str
    created_at: str


class CreditsOut(BaseModel):
    balance: float
    entries: list[CreditEntryOut]


class InvoiceLineOut(BaseModel):
    kind: str
    capability_id: str | None
    description: str
    quantity: float
    amount_cents: int


class InvoiceOut(BaseModel):
    id: str
    number: str
    period_key: str
    status: str
    currency: str
    total_cents: int
    finalized_at: str | None
    lines: list[InvoiceLineOut]


@router.get("/credits", response_model=CreditsOut)
async def get_credits(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CreditsOut:
    """Credit balance plus recent movements.

    The balance is SUM(delta) over an append-only ledger, never a stored counter, so what the
    customer sees is derived from the same rows an audit would read.
    """
    from nexus.billing.credits import balance, history

    entries = await history(ts, limit=50)
    return CreditsOut(
        balance=await balance(ts),
        entries=[
            CreditEntryOut(
                id=e.id, delta=float(e.delta), kind=e.kind, reason=e.reason,
                created_at=e.created_at.isoformat() if e.created_at else "",
            )
            for e in entries
        ],
    )


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[InvoiceOut]:
    """This workspace's invoices, newest period first, each with its charge lines."""
    from nexus.models.billing import BillingInvoice, BillingInvoiceLine

    invoices = await ts.list(BillingInvoice)
    invoices.sort(key=lambda i: i.period_key, reverse=True)
    all_lines = await ts.list(BillingInvoiceLine)
    by_invoice: dict[str, list] = {}
    for line in all_lines:
        by_invoice.setdefault(line.invoice_id, []).append(line)

    return [
        InvoiceOut(
            id=inv.id, number=inv.number, period_key=inv.period_key, status=inv.status,
            currency=inv.currency, total_cents=inv.total_cents,
            finalized_at=inv.finalized_at.isoformat() if inv.finalized_at else None,
            lines=[
                InvoiceLineOut(
                    kind=ln.kind, capability_id=ln.capability_id, description=ln.description,
                    quantity=float(ln.quantity), amount_cents=ln.amount_cents,
                )
                for ln in sorted(by_invoice.get(inv.id, []), key=lambda x: (x.kind, x.description))
            ],
        )
        for inv in invoices
    ]
