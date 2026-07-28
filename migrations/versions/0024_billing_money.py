"""Billing money layer: rate cards, cost rates, credit ledger, invoices.

Additive only — safe on a live database.

Revision ID: 0024_billing_money
Revises: 0023_billing_rollup_marker
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_billing_money"
down_revision = "0023_billing_rollup_marker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_rate_cards",
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("credits_per_unit", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("tiers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("margin_exception", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("margin_exception_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "billing_cost_rates",
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("unit_cost_usd", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "billing_credit_ledger",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("delta", sa.Numeric(14, 4), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("capability_id", sa.String(length=80), nullable=True),
        sa.Column("period_key", sa.String(length=40), nullable=True, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_idempotency"),
    )
    op.create_index("ix_credit_tenant_time", "billing_credit_ledger", ["tenant_id", "created_at"])
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("number", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("period_key", sa.String(length=40), nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan_id", sa.String(length=60), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "period_key", name="uq_invoice_period"),
    )
    op.create_index("ix_invoice_tenant_status", "billing_invoices", ["tenant_id", "status"])
    op.create_table(
        "billing_invoice_lines",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("invoice_id", sa.String(length=32),
                  sa.ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("capability_id", sa.String(length=80), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("unit_credits", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_invoice_line_invoice", "billing_invoice_lines", ["invoice_id"])


def downgrade() -> None:
    op.drop_table("billing_invoice_lines")
    op.drop_table("billing_invoices")
    op.drop_table("billing_credit_ledger")
    op.drop_table("billing_cost_rates")
    op.drop_table("billing_rate_cards")
