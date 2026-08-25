"""Per-tenant CRM credentials: crm_connections.

Moves CRM connectivity off deployment-global env vars (NEXUS_CRM_PROVIDER /
NEXUS_HUBSPOT_ACCESS_TOKEN) and onto a per-tenant, encrypted credential. Additive only: with no
rows, every tenant falls back to the env configuration exactly as before.

``secret`` holds a Fernet envelope, never plaintext. The table is tenant-scoped, so
``scripts/apply_rls.py`` applies an RLS policy to it on the next deploy with no manual work.

Revision ID: 0047_crm_connections
Revises: 0046_payment_credentials
Create Date: 2026-08-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Renumbered 0044_crm_connections -> 0047_crm_connections on 2026-08-25. This chain and master's
# (0044_provider_keys -> 0046_payment_credentials) both branched from
# 0043_signal_subtype, so merging them produced TWO alembic heads and
# `alembic upgrade head` refuses to run against those. Rebased onto master's head
# rather than adding a merge revision: neither of these had been applied anywhere,
# so there is no stamped database that remembers the old id.

revision = "0047_crm_connections"
down_revision = "0046_payment_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_connections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("secret", sa.JSON(), nullable=False),
        sa.Column("api_base", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("updated_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_crm_connection_tenant"),
    )
    op.create_index("ix_crm_connections_tenant_id", "crm_connections", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_crm_connections_tenant_id", table_name="crm_connections")
    op.drop_table("crm_connections")
