"""Forgot/reset password: password_resets (single-use, time-boxed reset tokens).

Revision ID: 0017_password_reset
Revises: 0016_pending_registration
Create Date: 2026-06-23

Holds a hashed, expiring reset token bound to a user; consumed (or overwritten) on use. Token is
stored only as an HMAC hash, never plaintext. The offline test path builds schema via
Base.metadata.create_all; this migration is for Postgres production.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_password_reset"
down_revision = "0016_pending_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_password_resets_email", "password_resets", ["email"], unique=True)
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_index("ix_password_resets_email", table_name="password_resets")
    op.drop_table("password_resets")
