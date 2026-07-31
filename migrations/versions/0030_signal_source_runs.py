"""Crawl history for signal sources.

Additive: a new tenant-scoped table, no changes to anything that exists. `scripts/apply_rls.py`
enrols it automatically on deploy because it carries a `tenant_id` — a crawl belongs to exactly one
workspace, unlike `dead_letter_jobs`, which deliberately avoids that column.

Revision ID: 0030_signal_source_runs
Revises: 0029_admin_permissions
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_signal_source_runs"
down_revision = "0029_admin_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_source_runs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("items_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signal_source_runs_account_id", "signal_source_runs", ["account_id"])
    op.create_index("ix_signal_source_runs_source", "signal_source_runs", ["source"])
    op.create_index("ix_signal_source_runs_outcome", "signal_source_runs", ["outcome"])
    op.create_index("ix_signal_source_runs_started_at", "signal_source_runs", ["started_at"])
    # The two questions actually asked of this table.
    op.create_index(
        "ix_source_run_account", "signal_source_runs", ["tenant_id", "account_id", "started_at"]
    )
    op.create_index(
        "ix_source_run_health", "signal_source_runs", ["tenant_id", "source", "started_at"]
    )


def downgrade() -> None:
    op.drop_table("signal_source_runs")
