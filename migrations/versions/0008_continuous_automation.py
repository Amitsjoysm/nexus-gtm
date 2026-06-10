"""Continuous Automation: per-tenant opt-in flag + account staleness timestamp.

Revision ID: 0008_continuous_automation
Revises: 0007_cadence
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_continuous_automation"
down_revision = "0007_cadence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column(
                "automation_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_accounts_last_refreshed_at", "accounts", ["last_refreshed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_last_refreshed_at", table_name="accounts")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("last_refreshed_at")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("automation_enabled")
