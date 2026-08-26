"""Runtime setting overrides, changeable from the Control plane.

`get_settings()` is an `lru_cache` over a mutable object, so an override applied with `setattr`
reaches all 142 call sites without any of them changing. That is the whole reason this can exist
without rewiring the application: the alternative — a resolver each call site has to adopt — would
have been 142 opportunities to miss one and leave a setting that silently ignores the panel.

No ``tenant_id``: deployment configuration, like `provider_settings` and `payment_credentials`.

Revision ID: 0050_runtime_settings
Revises: 0049_integration_connections
Create Date: 2026-08-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_runtime_settings"
down_revision = "0049_integration_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False, unique=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("updated_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
