"""Conversational orchestrator: chat sessions/messages, custom-field defs, run link, source.

Revision ID: 0002_conversational_orchestrator
Revises: 0001_initial
Create Date: 2026-06-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_conversational_orchestrator"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _ts_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("parent_session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False,
                  server_default="New conversation"),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="active"),
        sa.Column("target", sa.String(length=16), nullable=True),
        sa.Column("icp_state", sa.JSON(), nullable=True),
        sa.Column("missing_slots", sa.JSON(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"])
    op.create_index("ix_chat_sessions_account_id", "chat_sessions", ["account_id"])
    op.create_index(
        "ix_chat_session_tenant_account", "chat_sessions", ["tenant_id", "account_id"])
    op.create_index(
        "ix_chat_session_tenant_status", "chat_sessions", ["tenant_id", "status"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.UniqueConstraint("session_id", "seq", name="uq_chat_msg_seq"),
    )
    op.create_index("ix_chat_messages_tenant_id", "chat_messages", ["tenant_id"])
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_msg_session_seq", "chat_messages", ["session_id", "seq"])

    op.create_table(
        "custom_field_defs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        *_ts_columns(),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("entity", sa.String(length=12), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="text"),
        sa.UniqueConstraint("tenant_id", "entity", "key", name="uq_custom_field_key"),
    )
    op.create_index("ix_custom_field_defs_tenant_id", "custom_field_defs", ["tenant_id"])

    # New columns on existing tables.
    op.add_column("accounts", sa.Column("custom_fields", sa.JSON(), nullable=True))
    op.add_column("accounts", sa.Column("source", sa.String(length=40), nullable=True))
    op.add_column("contacts", sa.Column("custom_fields", sa.JSON(), nullable=True))
    op.add_column(
        "orchestration_runs",
        sa.Column("chat_session_id", sa.String(length=32),
                  sa.ForeignKey("chat_sessions.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orchestration_runs", "chat_session_id")
    op.drop_column("contacts", "custom_fields")
    op.drop_column("accounts", "source")
    op.drop_column("accounts", "custom_fields")
    op.drop_index("ix_custom_field_defs_tenant_id", table_name="custom_field_defs")
    op.drop_table("custom_field_defs")
    op.drop_index("ix_chat_msg_session_seq", table_name="chat_messages")
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_tenant_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_session_tenant_status", table_name="chat_sessions")
    op.drop_index("ix_chat_session_tenant_account", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_account_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_tenant_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
