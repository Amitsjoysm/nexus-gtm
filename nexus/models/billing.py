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
