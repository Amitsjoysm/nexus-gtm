"""Contact sourcing: campaigns.send_risky opt-in column.

Revision ID: 0006_contact_sourcing
Revises: 0005_campaigns
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_contact_sourcing"
down_revision = "0005_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(
            sa.Column(
                "send_risky", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("send_risky")
