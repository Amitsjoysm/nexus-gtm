"""Multi-factor authentication: enrolled second factors and recovery codes.

Additive. Both tables are **user-scoped, not tenant-scoped** — deliberately no ``tenant_id``
column. A user can belong to several workspaces and their second factor belongs to the identity,
not to a workspace; more importantly ``scripts/apply_rls.py`` enrols any table having a
``tenant_id`` into Row-Level Security, and these rows are read on the *login* path where no tenant
binding exists yet. Under a policy that read returns zero rows instead of erroring — MFA would
quietly disable itself. If a tenant reference is ever needed it must be called
``subject_tenant_id``, matching ``billing_audit_log`` / ``dead_letter_jobs``.

Revision ID: 0028_user_mfa
Revises: 0027_dead_letter_jobs
Create Date: 2026-07-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_user_mfa"
down_revision = "0027_dead_letter_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_mfa",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        # "totp" (authenticator app) or "email" (mailed code).
        sa.Column("method", sa.String(length=16), nullable=False),
        # Fernet ciphertext of the base32 seed — never the raw secret.
        sa.Column("secret", sa.Text(), nullable=False, server_default=""),
        # NULL until the first code is verified. An unconfirmed enrolment never gates login.
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        # Highest TOTP counter already accepted — the replay guard.
        sa.Column("last_used_counter", sa.Integer(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "method", name="uq_user_mfa_method"),
    )
    op.create_index("ix_user_mfa_user_id", "user_mfa", ["user_id"])

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        # One-way HMAC-SHA256 hex digest. The plaintext is shown once, at generation, and never
        # persisted anywhere.
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"])
    op.create_index("ix_mfa_recovery_codes_code_hash", "mfa_recovery_codes", ["code_hash"])


def downgrade() -> None:
    op.drop_table("mfa_recovery_codes")
    op.drop_table("user_mfa")
