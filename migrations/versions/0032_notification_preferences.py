"""Per-user notification routing.

Additive: one new tenant-scoped table, enrolled in RLS automatically by `scripts/apply_rls.py`.

Behaviour is unchanged until a user creates a row. The absence of a preference means "no preference
expressed", and the tenant-level channel configuration continues to apply exactly as before — so
this migration alone changes nobody's delivery.

Revision ID: 0032_notification_preferences
Revises: 0031_page_snapshots
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_notification_preferences"
down_revision = "0031_page_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="in_app"),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="immediate"),
        sa.Column("quiet_from_min", sa.Integer(), nullable=True),
        sa.Column("quiet_to_min", sa.Integer(), nullable=True),
        sa.Column("utc_offset_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quiet_hours_allow_critical", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # One row per user per category per channel: a UI that saves twice would otherwise produce
        # two contradictory preferences and make delivery a coin flip.
        sa.UniqueConstraint(
            "tenant_id", "user_id", "category", "channel", name="uq_notification_preference"
        ),
    )
    op.create_index("ix_notification_preferences_user_id", "notification_preferences", ["user_id"])
    op.create_index("ix_notification_preferences_category", "notification_preferences", ["category"])
    op.create_index(
        "ix_notification_pref_user", "notification_preferences", ["tenant_id", "user_id"]
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
