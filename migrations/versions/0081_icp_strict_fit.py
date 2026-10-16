"""tenants.icp_strict_fit: the workspace asked that every discovered account clear the fit score

Additive and off for every existing workspace, so nothing changes until an admin whose plan includes
``discovery.strict_fit`` turns it on.

Revision ID: 0081_icp_strict_fit
Revises: 0080_smtp_sends_recorded
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0081_icp_strict_fit"
down_revision = "0080_smtp_sends_recorded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("icp_strict_fit", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("tenants", "icp_strict_fit")
