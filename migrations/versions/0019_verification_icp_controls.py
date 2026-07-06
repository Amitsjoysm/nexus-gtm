"""SDR workflow hardening: contact verification timestamp + per-tenant ICP daily count.

Revision ID: 0019_verification_icp_controls
Revises: 0018_relationship_graph
Create Date: 2026-07-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_verification_icp_controls"
down_revision = "0018_relationship_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(
            sa.Column("email_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("icp_daily_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("icp_daily_count")
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("email_checked_at")
