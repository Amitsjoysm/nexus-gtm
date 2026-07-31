"""Feature-flag registry for entitlements.

Additive: one new platform-global table. No `tenant_id`, so `scripts/apply_rls.py` leaves it alone —
the same posture as `billing_capabilities` and `billing_plans`.

Behaviour is unchanged for every existing entitlement: `feature_flag` is null on all of them, and a
named-but-unregistered flag evaluates to ENABLED, so no capability can be switched off by this
migration alone.

Revision ID: 0033_billing_feature_flags
Revises: 0032_notification_preferences
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_billing_feature_flags"
down_revision = "0032_notification_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_feature_flags",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("overrides", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("billing_feature_flags")
