"""Workspace audit trail: audit_log.

Privileged, security-relevant actions a workspace admin can review — today the CRM/SEP connection
changes, which move a customer's data to a different destination and so must be attributable.

Tenant-scoped, unlike ``billing_audit_log``: the reader here is the workspace admin, not the
platform operator, so ``scripts/apply_rls.py`` enrolling it is the desired behaviour.

Revision ID: 0048_audit_log
Revises: 0047_crm_connections
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Renumbered 0045_audit_log -> 0048_audit_log on 2026-08-25. This chain and master's
# (0044_provider_keys -> 0046_payment_credentials) both branched from
# 0043_signal_subtype, so merging them produced TWO alembic heads and
# `alembic upgrade head` refuses to run against those. Rebased onto master's head
# rather than adding a merge revision: neither of these had been applied anywhere,
# so there is no stamped database that remembers the old id.

revision = "0048_audit_log"
down_revision = "0047_crm_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.String(32), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    # Matches the only read pattern: this tenant's trail, newest first.
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")
