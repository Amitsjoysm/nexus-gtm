"""proration adjustments

Mid-cycle plan changes produce a day-weighted credit and charge. They are stored rather than
applied to an invoice on the spot because ``rate_period`` rebuilds invoice lines from scratch —
a row here is read by every rating pass and never consumed, so re-rating cannot double-bill.

Tenant-scoped, so ``scripts/apply_rls.py`` enrols it automatically like every other table
carrying ``tenant_id``. No manual policy work.

Revision ID: 0036_proration_adjustments
Revises: 0035_companies
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_proration_adjustments"
down_revision = "0035_companies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_proration_adjustments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("period_key", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        # Signed: a credit is negative, so summing gives the net without special-casing kinds.
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("from_plan_id", sa.String(length=60), nullable=True),
        sa.Column("to_plan_id", sa.String(length=60), nullable=True),
        sa.Column("days_remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("days_in_period", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proration_tenant_period",
        "billing_proration_adjustments",
        ["tenant_id", "period_key"],
    )
    op.create_index(
        op.f("ix_billing_proration_adjustments_period_key"),
        "billing_proration_adjustments",
        ["period_key"],
    )
    op.create_index(
        op.f("ix_billing_proration_adjustments_tenant_id"),
        "billing_proration_adjustments",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_billing_proration_adjustments_tenant_id"),
        table_name="billing_proration_adjustments",
    )
    op.drop_index(
        op.f("ix_billing_proration_adjustments_period_key"),
        table_name="billing_proration_adjustments",
    )
    op.drop_index("ix_proration_tenant_period", table_name="billing_proration_adjustments")
    op.drop_table("billing_proration_adjustments")
