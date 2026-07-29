# nexus/models/billing.py
"""Billing platform: catalog, plans, entitlements, subscriptions, platform admins.

Two classes of table live here:

* **Platform-global config** (``BillingCapability``, ``BillingPlan``,
  ``BillingPlanEntitlement``, ``PlatformAdmin``) — one shared catalog/price list for the whole
  platform. Deliberately NOT ``TenantScoped``: there is no tenant dimension, and every tenant
  reads the same rows. Writes are admin-only at the API layer.
* **Tenant-owned state** (``BillingSubscription``) — ``TenantScoped``, so
  ``scripts/apply_rls.py`` picks it up automatically and Postgres RLS isolates it like every
  other tenant table.

IDs are human-readable slugs (``ai.email_draft``, ``growth``) rather than UUIDs: they appear in
application code, admin URLs, and invoices, and must stay stable and greppable.
See docs/billing/01-Billing-Architecture.md §4 and 08-Feature-Catalog.md.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# ---- vocabularies (validated at the API layer; stored as plain strings for forward-compat) ----
CAPABILITY_UNITS = (
    "action", "token", "search", "check", "message", "seat", "gb", "minute",
    "request", "job", "run",
)
METER_KINDS = ("counter", "gauge", "passthrough")
# shadow = record only; enabled = allowed, unmetered; metered = allowed + counted against quota;
# enterprise = off unless a contract/plan turns it on; disabled = blocked.
CAPABILITY_MODES = ("shadow", "enabled", "metered", "enterprise", "disabled")
PLAN_CLASSES = (
    "free", "trial", "standard", "usage", "hybrid", "unlimited",
    "enterprise", "custom", "partner", "internal",
)
PLAN_STATUSES = ("draft", "active", "grandfathered", "retired")
SUBSCRIPTION_STATUSES = (
    "trialing", "active", "past_due", "suspended", "canceled",
)
RESET_POLICIES = ("monthly_anniversary", "calendar_month", "daily", "never")


class BillingCapability(TimestampMixin, Base):
    """One billable thing. The stable ID application code will reference at the metering seam."""

    __tablename__ = "billing_capabilities"
    __table_args__ = (Index("ix_billing_cap_category", "category", "sub_category"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)   # e.g. "ai.email_draft"
    category: Mapped[str] = mapped_column(String(40), index=True)   # ai | search | outreach | ...
    sub_category: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(20), default="action")
    meter_kind: Mapped[str] = mapped_column(String(20), default="counter")
    default_mode: Mapped[str] = mapped_column(String(20), default="shadow")
    # Capabilities this one requires (module gates, e.g. ["module.network"]).
    depends_on: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Retired capabilities stay in the table (invoices reference them) but stop being offered.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BillingPlan(TimestampMixin, Base):
    """A sellable plan. Behavior is entirely data — no plan-specific code anywhere."""

    __tablename__ = "billing_plans"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)   # e.g. "growth"
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    plan_class: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # Money is stored in integer minor units (cents) — never floats.
    base_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seat_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    interval: Mapped[str] = mapped_column(String(10), default="month")  # month | year
    included_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Ordering on the pricing page / admin lists.
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    # Free-form extras (rollover_months, support_level, ...) so new plan knobs need no migration.
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BillingPlanEntitlement(IdMixin, TimestampMixin, Base):
    """plan x capability -> the full policy surface (docs/billing/02-Entitlement-Engine.md §1)."""

    __tablename__ = "billing_plan_entitlements"
    __table_args__ = (
        UniqueConstraint("plan_id", "capability_id", name="uq_plan_entitlement"),
        Index("ix_plan_entitlement_plan", "plan_id"),
    )

    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id", ondelete="CASCADE"))
    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE")
    )
    mode: Mapped[str] = mapped_column(String(20), default="metered")
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)        # None = unlimited
    soft_limit_pct: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    hard_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reset_policy: Mapped[str] = mapped_column(String(30), default="monthly_anniversary")
    burst_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)   # per rolling minute
    rate_limit: Mapped[str | None] = mapped_column(String(20), nullable=True)  # e.g. "60/min"
    cooldown_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overage_price_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feature_flag: Mapped[str | None] = mapped_column(String(60), nullable=True)
    trial_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BillingSubscription(IdMixin, TimestampMixin, TenantScoped, Base):
    """A tenant's current plan + billing period. One active row per tenant."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (Index("ix_billing_sub_tenant_status", "tenant_id", "status"),)

    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    interval: Mapped[str] = mapped_column(String(10), default="month")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    current_period_start: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    trial_end: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Frozen terms: plan edits must never reprice a grandfathered subscriber.
    grandfathered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seats_included: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Payment-provider linkage; None for internal/legacy plans that never touch a PSP.
    psp_customer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    psp_subscription_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class PlatformAdmin(IdMixin, TimestampMixin, Base):
    """Staff access to /admin — deliberately separate from tenant RBAC.

    A platform admin is an operator of the SaaS, not a member of any customer workspace; the two
    authorization systems must never be conflated (docs/billing/06-Admin-Portal.md §1).
    """

    __tablename__ = "platform_admins"
    __table_args__ = (UniqueConstraint("email", name="uq_platform_admin_email"),)

    email: Mapped[str] = mapped_column(String(255), index=True)
    # super | finance | support | ops | sales
    platform_role: Mapped[str] = mapped_column(String(20), default="support")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")


class BillingUsageEvent(IdMixin, TimestampMixin, TenantScoped, Base):
    """One immutable, idempotent record of a billable action.

    This is the system of truth for billing: invoices, quotas, and margin reports are all
    derived from this stream (docs/billing/03-Metering-Architecture.md §1). The billing FACTS
    (quantity, capability, cost, timestamps) are never updated or deleted inside the retention
    window — corrections are compensating rows with a negative ``quantity``. The one writable
    field is ``rolled_at``, which is derived bookkeeping, not a billing fact.

    ``unit_cost_usd`` is stamped AT WRITE TIME from the cost-rate table so margin reports reflect
    the cost when the action happened, immune to later provider repricing.
    """

    __tablename__ = "billing_usage_events"
    __table_args__ = (
        # Replay safety: a retried queue job or duplicated webhook can never double-bill.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idempotency"),
        Index("ix_usage_tenant_cap_time", "tenant_id", "capability_id", "occurred_at"),
        Index("ix_usage_occurred", "occurred_at"),
        # The quota hot path scans exactly the unrolled tail; keep it index-only.
        Index("ix_usage_unrolled", "tenant_id", "capability_id", "rolled_at"),
    )

    capability_id: Mapped[str] = mapped_column(String(80), index=True)
    # Numeric, not int: tokens and GB are fractional units.
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="action")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="api")  # api|worker|middleware|system
    idempotency_key: Mapped[str] = mapped_column(String(120))
    attrs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    unit_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)
    billed_credits: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    # NULL until a rollup has folded this event in. Quota reads are "period rollup + events
    # still NULL", which is exact without comparing two Python-stamped clocks: on a coarse
    # clock (Windows ticks at ~15ms) a real event can tie the rollup's write time and vanish
    # from the count, which under enforcement hands out free quota. A marker cannot tie.
    # It is also liveness-safe: if the rollup worker stops, everything stays NULL and is summed
    # live — the read degrades to slower, never to undercounting.
    rolled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class BillingUsageRollup(IdMixin, TimestampMixin, TenantScoped, Base):
    """Pre-aggregated usage per tenant/capability/period — the fast path for quota checks and
    dashboards. Derived from events; safe to rebuild at any time."""

    __tablename__ = "billing_usage_rollups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "capability_id", "period_kind", "period_key",
            name="uq_usage_rollup_period",
        ),
        Index("ix_usage_rollup_lookup", "tenant_id", "capability_id", "period_kind"),
    )

    capability_id: Mapped[str] = mapped_column(String(80), index=True)
    period_kind: Mapped[str] = mapped_column(String(10))       # hour | day | period
    period_key: Mapped[str] = mapped_column(String(40))        # "2026-07-28T14" | "2026-07-28"
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 8), default=0, nullable=False)


# ---- money: rate cards, costs, credits, invoices ---------------------------------------------
class BillingRateCard(TimestampMixin, Base):
    """What one unit of a capability COSTS THE CUSTOMER, in credits (1 credit = $0.01 list).

    Platform-global config, edited in Admin. ``tiers`` is an ordered volume ladder:
    ``[{"upto": 10000, "credits": 2}, {"upto": null, "credits": 1}]`` — the last entry with a
    null ``upto`` is the catch-all. Empty tiers ⇒ flat ``credits_per_unit``.
    """

    __tablename__ = "billing_rate_cards"

    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE"), primary_key=True
    )
    credits_per_unit: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    tiers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Set when finance deliberately ships a line below the margin floor; surfaces on dashboards.
    margin_exception: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    margin_exception_reason: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BillingCostRate(TimestampMixin, Base):
    """What one unit of a capability COSTS US (COGS). Drives margin validation and reporting.

    Versioned by ``updated_at``; usage events stamp the cost at write time so historical margin
    is immune to later repricing (docs/billing/12-Cost-Analysis.md).
    """

    __tablename__ = "billing_cost_rates"

    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE"), primary_key=True
    )
    unit_cost_usd: Mapped[float] = mapped_column(Numeric(12, 8), default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(120), default="")   # provider//model provenance
    note: Mapped[str] = mapped_column(Text, default="")


class BillingCreditLedger(IdMixin, TimestampMixin, TenantScoped, Base):
    """Append-only credit movements. Balance is SUM(delta) — never a mutable counter.

    An append-only ledger is the only representation that survives concurrent grants/burns and
    stays auditable when a customer disputes a charge.
    """

    __tablename__ = "billing_credit_ledger"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_idempotency"),
        Index("ix_credit_tenant_time", "tenant_id", "created_at"),
    )

    # Positive = granted/purchased, negative = burned.
    delta: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    kind: Mapped[str] = mapped_column(String(20))   # grant|purchase|burn|expiry|adjustment
    reason: Mapped[str] = mapped_column(Text, default="")
    capability_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    period_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(120), default="system")


class BillingInvoice(IdMixin, TimestampMixin, TenantScoped, Base):
    """A rated billing period. Immutable once finalized — corrections are credit notes."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_key", name="uq_invoice_period"),
        Index("ix_invoice_tenant_status", "tenant_id", "status"),
    )

    number: Mapped[str] = mapped_column(String(40), default="")
    period_key: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|finalized|paid|void
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BillingInvoiceLine(IdMixin, TimestampMixin, TenantScoped, Base):
    """One charge on an invoice. Always traceable to a capability + period."""

    __tablename__ = "billing_invoice_lines"
    __table_args__ = (Index("ix_invoice_line_invoice", "invoice_id"),)

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("billing_invoices.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))   # base|seat|overage|credit_pack|discount
    capability_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    unit_credits: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BillingAuditLog(IdMixin, TimestampMixin, Base):
    """Every platform-admin mutation, with before/after snapshots.

    Required by docs/billing/17-Production-Checklist.md §Admin: 100% of admin mutations
    captured. Without it, a platform admin can reprice a plan and nothing records who or when.

    Platform-global on purpose, like the catalog: it records actions ACROSS tenants and must
    stay readable by a platform admin who has no tenant binding. The affected tenant is stored
    as ``subject_tenant_id`` rather than ``tenant_id`` specifically so that
    ``scripts/apply_rls.py`` — which enrolls any table having a ``tenant_id`` column — does not
    put an RLS policy on it and hide the log from the only people meant to read it.
    """

    __tablename__ = "billing_audit_log"
    __table_args__ = (
        Index("ix_billing_audit_action_time", "action", "created_at"),
        Index("ix_billing_audit_subject", "subject_tenant_id"),
    )

    actor: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str] = mapped_column(String(120), default="")
    subject_tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
