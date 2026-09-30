"""page visibility: a superadmin hides a menu page from every workspace

One row per page a superadmin has touched. No row means shown, so this changes nothing until
someone hides a page. Platform-global (no ``tenant_id``), like ``feature_switches``.

Revision ID: 0066_page_visibility
Revises: 0065_provider_base_url
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_page_visibility"
down_revision = "0065_provider_base_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_visibility",
        sa.Column("page_key", sa.String(length=40), primary_key=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("page_visibility")
