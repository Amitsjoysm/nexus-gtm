"""Per-workspace outbound email (SMTP) settings on the tenant.

Revision ID: 0012_email_settings
Revises: 0011_sdr_adoption
Create Date: 2026-06-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_email_settings"
down_revision = "0011_sdr_adoption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column(
                "email_settings",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("email_settings")
