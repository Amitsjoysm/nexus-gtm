# migrations/versions/0021_billing_foundation.py
"""Billing foundation: capability catalog, plans, entitlements, subscriptions, platform admins.

Additive only — no existing table is touched, so this is safe to apply to a live database with
zero downtime (docs/billing/15-Migration-Strategy.md).

Revision ID: 0021_billing_foundation
Revises: 0020_account_archived_at
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_billing_foundation"
down_revision = "0020_account_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_capabilities",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("category", sa.String(length=40), nullable=False, index=True),
        sa.Column("sub_category", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="action"),
        sa.Column("meter_kind", sa.String(length=20), nullable=False, server_default="counter"),
        sa.Column("default_mode", sa.String(length=20), nullable=False, server_default="shadow"),
        sa.Column("depends_on", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_billing_cap_category", "billing_capabilities", ["category", "sub_category"])

    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_class", sa.String(length=20), nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft",
                  index=True),
        sa.Column("base_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seat_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("interval", sa.String(length=10), nullable=False, server_default="month"),
        sa.Column("included_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_seats", sa.Integer(), nullable=True),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "billing_plan_entitlements",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("plan_id", sa.String(length=60),
                  sa.ForeignKey("billing_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="metered"),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("soft_limit_pct", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("hard_limit", sa.Integer(), nullable=True),
        sa.Column("reset_policy", sa.String(length=30), nullable=False,
                  server_default="monthly_anniversary"),
        sa.Column("burst_limit", sa.Integer(), nullable=True),
        sa.Column("rate_limit", sa.String(length=20), nullable=True),
        sa.Column("cooldown_s", sa.Integer(), nullable=True),
        sa.Column("overage_price_credits", sa.Integer(), nullable=True),
        sa.Column("feature_flag", sa.String(length=60), nullable=True),
        sa.Column("trial_quota", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("plan_id", "capability_id", name="uq_plan_entitlement"),
    )
    op.create_index("ix_plan_entitlement_plan", "billing_plan_entitlements", ["plan_id"])

    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("plan_id", sa.String(length=60), sa.ForeignKey("billing_plans.id"),
                  nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active",
                  index=True),
        sa.Column("interval", sa.String(length=10), nullable=False, server_default="month"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("grandfathered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("seats_included", sa.Integer(), nullable=True),
        sa.Column("psp_customer_id", sa.String(length=120), nullable=True),
        sa.Column("psp_subscription_id", sa.String(length=120), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_billing_sub_tenant_status", "billing_subscriptions",
                    ["tenant_id", "status"])

    op.create_table(
        "platform_admins",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False, index=True),
        sa.Column("platform_role", sa.String(length=20), nullable=False,
                  server_default="support"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_platform_admin_email"),
    )


def downgrade() -> None:
    op.drop_table("platform_admins")
    op.drop_table("billing_plan_entitlements")
    op.drop_table("billing_subscriptions")
    op.drop_table("billing_capabilities")
    op.drop_table("billing_plans")
