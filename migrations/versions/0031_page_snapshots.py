"""Baselines for website change monitoring.

Additive: one new tenant-scoped table. `scripts/apply_rls.py` enrols it automatically because it
carries a `tenant_id` — two workspaces watching the same company must keep independent baselines,
since they started watching on different days.

Revision ID: 0031_page_snapshots
Revises: 0030_signal_source_runs
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_page_snapshots"
down_revision = "0030_signal_source_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_snapshots",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=False),
        sa.Column("page_kind", sa.String(length=20), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_change_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # One baseline per page kind per account per tenant: this is what makes a re-check
        # idempotent rather than accumulating rows and diffing against an arbitrary one.
        sa.UniqueConstraint("tenant_id", "account_id", "page_kind", name="uq_page_snapshot"),
    )
    op.create_index("ix_page_snapshots_account_id", "page_snapshots", ["account_id"])
    op.create_index("ix_page_snapshots_page_kind", "page_snapshots", ["page_kind"])
    op.create_index("ix_page_snapshots_content_hash", "page_snapshots", ["content_hash"])
    op.create_index(
        "ix_page_snapshot_changed", "page_snapshots", ["tenant_id", "last_changed_at"]
    )


def downgrade() -> None:
    op.drop_table("page_snapshots")
