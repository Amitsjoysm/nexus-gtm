"""mcp: connected AI apps (API keys and OAuth grants) and NEXUS's OAuth server tables

All four tables are platform-global: a bearer is resolved before any workspace is known, so a row
level security policy would hide them from the very lookup that needs them. The workspace a
connection belongs to is `subject_tenant_id`, which `scripts/apply_rls.py` does not enrol.

Revision ID: 0068_mcp
Revises: 0067_stripe_manage
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_mcp"
down_revision = "0067_stripe_manage"
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("client_id", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("key_digest", sa.String(length=64), nullable=True, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=32), nullable=False, server_default=""),
        *_stamps(),
    )
    op.create_index("ix_mcp_connections_subject_tenant_id", "mcp_connections",
                    ["subject_tenant_id"])
    op.create_index("ix_mcp_connections_user_id", "mcp_connections", ["user_id"])

    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("client_uri", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("logo_uri", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("client_secret_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("metadata_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_oauth_clients_client_id", "oauth_clients", ["client_id"], unique=True)

    op.create_table(
        "oauth_codes",
        sa.Column("code_digest", sa.String(length=64), primary_key=True),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("connection_id", sa.String(length=32), sa.ForeignKey("mcp_connections.id"),
                  nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_oauth_codes_connection_id", "oauth_codes", ["connection_id"])

    op.create_table(
        "oauth_tokens",
        sa.Column("token_digest", sa.String(length=64), primary_key=True),
        sa.Column("connection_id", sa.String(length=32), sa.ForeignKey("mcp_connections.id"),
                  nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("family_id", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_oauth_tokens_connection_id", "oauth_tokens", ["connection_id"])
    op.create_index("ix_oauth_tokens_family_id", "oauth_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_clients")
    op.drop_table("mcp_connections")
