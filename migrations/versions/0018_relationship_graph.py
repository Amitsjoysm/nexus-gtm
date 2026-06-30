"""Relationship Graph (A1–A5): network source accounts, persons, identities, edges.

Revision ID: 0018_relationship_graph
Revises: 0017_password_reset
Create Date: 2026-06-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_relationship_graph"
down_revision = "0017_password_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "network_source_accounts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("member_id", sa.String(length=32), sa.ForeignKey("memberships.id"), index=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id")),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_account_id", sa.String(length=255), nullable=False),
        sa.Column("display_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="connected"),
        sa.Column("pooling_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("oauth", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sync_cursor", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "member_id", "provider", "external_account_id", name="uq_network_source"
        ),
    )
    op.create_index("ix_network_source_member", "network_source_accounts", ["tenant_id", "member_id"])

    op.create_table(
        "network_persons",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("primary_email", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("first_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("last_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("company", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("company_domain", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("location", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("linkedin_url", sa.String(length=300), nullable=True),
        sa.Column("twitter_handle", sa.String(length=100), nullable=True),
        sa.Column("photo_url", sa.String(length=500), nullable=True),
        sa.Column("profile", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("search_text", sa.String(length=600), nullable=False, server_default="", index=True),
        sa.Column("identity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("edge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_network_person_email", "network_persons", ["tenant_id", "primary_email"])
    op.create_index("ix_network_person_domain", "network_persons", ["tenant_id", "company_domain"])

    op.create_table(
        "network_identities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("source_account_id", sa.String(length=32),
                  sa.ForeignKey("network_source_accounts.id"), index=True),
        sa.Column("person_id", sa.String(length=32), sa.ForeignKey("network_persons.id"), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("handle", sa.String(length=100), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resolution_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "source_account_id", "external_id", name="uq_network_identity"),
    )
    op.create_index("ix_network_identity_key", "network_identities", ["tenant_id", "resolution_key"])
    op.create_index("ix_network_identity_person", "network_identities", ["tenant_id", "person_id"])

    op.create_table(
        "network_edges",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("owner_member_id", sa.String(length=32), sa.ForeignKey("memberships.id"), index=True),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id")),
        sa.Column("person_id", sa.String(length=32), sa.ForeignKey("network_persons.id"), index=True),
        sa.Column("source_account_id", sa.String(length=32),
                  sa.ForeignKey("network_source_accounts.id")),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False, server_default="contact"),
        sa.Column("strength", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("email_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meeting_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mutual_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pooling_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "owner_member_id", "person_id", "provider", name="uq_network_edge"),
    )
    op.create_index(
        "ix_network_edge_person", "network_edges",
        ["tenant_id", "person_id", "pooling_enabled", "strength"],
    )
    op.create_index("ix_network_edge_owner", "network_edges", ["tenant_id", "owner_member_id"])


def downgrade() -> None:
    op.drop_table("network_edges")
    op.drop_table("network_identities")
    op.drop_table("network_persons")
    op.drop_table("network_source_accounts")
