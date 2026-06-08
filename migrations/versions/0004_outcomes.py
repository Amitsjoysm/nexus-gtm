"""Outcome-feedback loop: the ``outcomes`` capture table.

Revision ID: 0004_outcomes
Revises: 0003_contact_email_status
Create Date: 2026-06-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_outcomes"
down_revision = "0003_contact_email_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outcomes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("contact_id", sa.String(length=32),
                  sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("tech_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meta", sa.JSON(), nullable=True),
    )
    op.create_index("ix_outcomes_tenant_id", "outcomes", ["tenant_id"])
    op.create_index("ix_outcomes_stage", "outcomes", ["stage"])
    op.create_index("ix_outcomes_account_id", "outcomes", ["account_id"])
    op.create_index("ix_outcomes_tenant_stage", "outcomes", ["tenant_id", "stage"])


def downgrade() -> None:
    op.drop_index("ix_outcomes_tenant_stage", table_name="outcomes")
    op.drop_index("ix_outcomes_account_id", table_name="outcomes")
    op.drop_index("ix_outcomes_stage", table_name="outcomes")
    op.drop_index("ix_outcomes_tenant_id", table_name="outcomes")
    op.drop_table("outcomes")
