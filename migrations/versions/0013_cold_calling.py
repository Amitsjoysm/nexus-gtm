"""Cold calling: call_tasks (queue) + call_activities (logged outcomes).

Revision ID: 0013_cold_calling
Revises: 0012_email_settings
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_cold_calling"
down_revision = "0012_email_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'open'")),
        sa.Column("source", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cadence_enrollment_id", sa.String(length=32),
                  sa.ForeignKey("cadence_enrollments.id"), nullable=True),
        sa.Column("cadence_step_index", sa.Integer(), nullable=True),
        sa.Column("script_cache", sa.JSON(), nullable=True),
    )
    op.create_index("ix_call_task_status", "call_tasks", ["tenant_id", "status"])
    op.create_index("ix_call_task_contact", "call_tasks", ["contact_id"])
    op.create_index("ix_call_tasks_account_id", "call_tasks", ["account_id"])

    op.create_table(
        "call_activities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("call_task_id", sa.String(length=32), sa.ForeignKey("call_tasks.id"), nullable=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("disposition", sa.String(length=24), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(length=16), nullable=True),
        sa.Column("provider_call_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_call_activity_contact", "call_activities", ["contact_id"])
    op.create_index("ix_call_activity_account", "call_activities", ["account_id"])


def downgrade() -> None:
    op.drop_table("call_activities")
    op.drop_table("call_tasks")
