"""Billing: platform-admin audit log.

Additive. Platform-global by design — the affected tenant is stored as ``subject_tenant_id``,
NOT ``tenant_id``, so ``scripts/apply_rls.py`` (which enrols any table having a ``tenant_id``
column) does not attach an RLS policy and hide the log from the platform admins who are the
only intended readers.

Revision ID: 0025_billing_audit
Revises: 0024_billing_money
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_billing_audit"
down_revision = "0024_billing_money"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_audit_log",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("target", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=True),
        sa.Column("before", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("after", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_billing_audit_log_actor", "billing_audit_log", ["actor"])
    op.create_index("ix_billing_audit_log_action", "billing_audit_log", ["action"])
    op.create_index(
        "ix_billing_audit_action_time", "billing_audit_log", ["action", "created_at"]
    )
    op.create_index("ix_billing_audit_subject", "billing_audit_log", ["subject_tenant_id"])


def downgrade() -> None:
    op.drop_table("billing_audit_log")
