"""Per-provider settings, starting with the LLM model.

The model is a provider-level choice, not a per-key one — every key for a provider talks to the
same catalogue — so it gets its own tiny table rather than a column on `provider_keys`.

This exists because of a measured outage: `llama-3.3-70b-versatile` was withdrawn by Groq, all five
keys returned 404, every LLM call fell to the stub, and the stub's copy went out as real outreach.
Fixing that meant editing deploy/.env and redeploying. Now it is a dropdown.

No ``tenant_id``: deployment-wide, like `provider_keys`.

Revision ID: 0045_provider_settings
Revises: 0044_provider_keys
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_provider_settings"
down_revision = "0044_provider_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_settings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, unique=True),
        sa.Column("model", sa.String(120), nullable=False, server_default=""),
        sa.Column("updated_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("provider_settings")
