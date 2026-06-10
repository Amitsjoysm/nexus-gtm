"""CRM Auto-Sync: add accounts.crm_synced_at + index.

Revision ID: 0009_crm_auto_sync
Revises: 0008_continuous_automation
Create Date: 2026-06-10

Note: the offline test path builds schema via Base.metadata.create_all (not alembic upgrade
head), matching migrations 0005-0008. This migration is for Postgres production; it is verified
by the revision-chain assertion in tests/test_crm_auto_sync.py, not by running upgrade head on
SQLite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_crm_auto_sync"
down_revision = "0008_continuous_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("crm_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_accounts_crm_synced_at", "accounts", ["crm_synced_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_crm_synced_at", table_name="accounts")
    op.drop_column("accounts", "crm_synced_at")
