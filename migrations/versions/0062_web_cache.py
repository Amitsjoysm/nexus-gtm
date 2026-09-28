"""web_cache: one shared, expiring copy of every web answer we buy

Signal collection re-asks the same four questions about the same company every six hours, per
tenant. This stores the answer with an expiry, so the repeat is served from our own database.

Platform-global (no ``tenant_id``), like ``companies`` and ``people``: ``scripts/apply_rls.py``
enrols anything with one, and an enrolled cache would return zero rows to the shared reader.

Additive and empty on upgrade. An empty cache behaves exactly as today: every lookup misses and the
existing provider is called.

Revision ID: 0062_web_cache
Revises: 0061_calling_billing
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_web_cache"
down_revision = "0061_calling_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_cache",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="search"),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("engine", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_web_cache_expires", "web_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_web_cache_expires", table_name="web_cache")
    op.drop_table("web_cache")
