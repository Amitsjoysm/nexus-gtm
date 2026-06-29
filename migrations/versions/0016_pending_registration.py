"""Two-step OTP registration: pending_registrations (signup awaiting email verification).

Revision ID: 0016_pending_registration
Revises: 0015_scale_indexes
Create Date: 2026-06-23

Holds an unverified signup (password already bcrypt-hashed, OTP stored only as an HMAC hash)
until the emailed code is verified, at which point it becomes a real Tenant/User/Workspace and the
row is deleted. Not tenant-scoped — no tenant exists yet. The offline test path builds schema via
Base.metadata.create_all; this migration is for Postgres production.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_pending_registration"
down_revision = "0015_scale_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_registrations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("company_slug", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("otp_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("resends", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pending_registrations_email", "pending_registrations", ["email"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_pending_registrations_email", table_name="pending_registrations")
    op.drop_table("pending_registrations")
