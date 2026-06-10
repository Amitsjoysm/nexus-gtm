"""Channel & Cadence: cadence tables + campaign cadence columns.

Revision ID: 0007_cadence
Revises: 0006_contact_sourcing
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_cadence"
down_revision = "0006_contact_sourcing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cadences",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_by_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_cadence_tenant", "cadences", ["tenant_id"])

    op.create_table(
        "cadence_steps",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("cadence_id", sa.String(length=32),
                  sa.ForeignKey("cadences.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("angle", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="email"),
        sa.UniqueConstraint("cadence_id", "step_index", name="uq_cadence_step_index"),
    )
    op.create_index("ix_cadence_steps_tenant_id", "cadence_steps", ["tenant_id"])
    op.create_index("ix_cadence_steps_cadence_id", "cadence_steps", ["cadence_id"])

    op.create_table(
        "cadence_enrollments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32),
                  sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("campaign_target_id", sa.String(length=32),
                  sa.ForeignKey("campaign_targets.id"), nullable=True),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("contact_id", sa.String(length=32),
                  sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("cadence_id", sa.String(length=32),
                  sa.ForeignKey("cadences.id"), nullable=False),
        sa.Column("current_step_index", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="active"),
        sa.Column("stop_reason", sa.String(length=16), nullable=True),
        sa.Column("next_touch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cadence_enrollments_tenant_id", "cadence_enrollments", ["tenant_id"])
    op.create_index("ix_cadence_enrollments_account_id", "cadence_enrollments", ["account_id"])
    op.create_index("ix_cadence_enrollments_cadence_id", "cadence_enrollments", ["cadence_id"])
    op.create_index("ix_cadence_enrollments_status", "cadence_enrollments", ["status"])
    op.create_index("ix_enrollment_campaign", "cadence_enrollments", ["campaign_id"])
    op.create_index(
        "ix_enrollment_status_due", "cadence_enrollments", ["status", "next_touch_at"]
    )

    op.create_table(
        "cadence_touches",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("cadence_enrollments.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("enrollment_id", "step_index", name="uq_touch_enrollment_step"),
    )
    op.create_index("ix_cadence_touches_tenant_id", "cadence_touches", ["tenant_id"])
    op.create_index("ix_cadence_touches_enrollment_id", "cadence_touches", ["enrollment_id"])

    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(
            sa.Column("cadence_id", sa.String(length=32), nullable=True)
        )
        batch.add_column(
            sa.Column("review_each_touch", sa.Boolean(), nullable=False,
                      server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("review_each_touch")
        batch.drop_column("cadence_id")
    op.drop_index("ix_cadence_touches_enrollment_id", table_name="cadence_touches")
    op.drop_index("ix_cadence_touches_tenant_id", table_name="cadence_touches")
    op.drop_table("cadence_touches")
    op.drop_index("ix_enrollment_status_due", table_name="cadence_enrollments")
    op.drop_index("ix_enrollment_campaign", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_status", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_cadence_id", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_account_id", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_tenant_id", table_name="cadence_enrollments")
    op.drop_table("cadence_enrollments")
    op.drop_index("ix_cadence_steps_cadence_id", table_name="cadence_steps")
    op.drop_index("ix_cadence_steps_tenant_id", table_name="cadence_steps")
    op.drop_table("cadence_steps")
    op.drop_index("ix_cadence_tenant", table_name="cadences")
    op.drop_table("cadences")
