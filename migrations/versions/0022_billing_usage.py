# migrations/versions/0022_billing_usage.py
"""Billing usage events + rollups.

Additive only. ``billing_usage_events`` is the highest-volume table in the platform; it ships
with the composite indexes the quota path and dashboards need from day one.

Revision ID: 0022_billing_usage
Revises: 0021_billing_foundation
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_billing_usage"
down_revision = "0021_billing_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_usage_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("capability_id", sa.String(length=80), nullable=False, index=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="action"),
        sa.Column("user_id", sa.String(length=32), nullable=True, index=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="api"),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("attrs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("unit_cost_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("billed_credits", sa.Numeric(12, 4), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idempotency"),
    )
    op.create_index(
        "ix_usage_tenant_cap_time", "billing_usage_events",
        ["tenant_id", "capability_id", "occurred_at"],
    )
    op.create_index("ix_usage_occurred", "billing_usage_events", ["occurred_at"])

    op.create_table(
        "billing_usage_rollups",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("capability_id", sa.String(length=80), nullable=False, index=True),
        sa.Column("period_kind", sa.String(length=10), nullable=False),
        sa.Column("period_key", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "capability_id", "period_kind", "period_key",
            name="uq_usage_rollup_period",
        ),
    )
    op.create_index(
        "ix_usage_rollup_lookup", "billing_usage_rollups",
        ["tenant_id", "capability_id", "period_kind"],
    )


def downgrade() -> None:
    op.drop_table("billing_usage_rollups")
    op.drop_table("billing_usage_events")
