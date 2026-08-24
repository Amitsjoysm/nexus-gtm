"""Platform-wide provider API keys.

Additive, and inert on its own: with no rows every provider resolves to its environment variable
exactly as before, so this migration changes no behaviour until an operator adds a key.

No ``tenant_id`` — deployment-wide credentials, not tenant data. ``scripts/apply_rls.py`` enrols
any table carrying ``tenant_id``, and enrolling this one would make every worker read return zero
rows silently.

Revision ID: 0044_provider_keys
Revises: 0043_signal_subtype
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_provider_keys"
down_revision = "0043_signal_subtype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("label", sa.String(80), nullable=False, server_default=""),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="untested"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_depth", sa.String(8), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.Column("last_error_status", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "key_digest", name="uq_provider_key_digest"),
    )
    op.create_index("ix_provider_keys_provider", "provider_keys", ["provider"])
    op.create_index("ix_provider_key_lookup", "provider_keys", ["provider", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_provider_key_lookup", table_name="provider_keys")
    op.drop_index("ix_provider_keys_provider", table_name="provider_keys")
    op.drop_table("provider_keys")
