"""Daily ICP auto-discovery: per-tenant last-run gate.

Revision ID: 0014_icp_auto_discovery
Revises: 0013_cold_calling
Create Date: 2026-06-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_icp_auto_discovery"
down_revision = "0013_cold_calling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column("icp_discovery_last_run_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("icp_discovery_last_run_at")
