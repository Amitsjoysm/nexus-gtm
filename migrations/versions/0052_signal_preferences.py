"""Per-workspace signal kind preferences.

A tester asked why signals they had never enabled were appearing, and whether they were being
billed for them. `signal_sources` is deployment-global and names which *collectors* run; there was
no per-tenant control over what gets kept.

Tenant-scoped, so `scripts/apply_rls.py` enrols it. Empty for every existing workspace, and an
ABSENT ROW MEANS ENABLED — so creating this table mutes nobody and changes nothing until somebody
opts out. Same rule as `notification_preferences`.

Revision ID: 0052_signal_preferences
Revises: 0051_account_geo_revenue
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_signal_preferences"
down_revision = "0051_account_geo_revenue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "kind", name="uq_signal_pref_kind"),
    )
    op.create_index("ix_signal_preferences_tenant_id", "signal_preferences", ["tenant_id"])
    op.create_index("ix_signal_preferences_kind", "signal_preferences", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_signal_preferences_kind", table_name="signal_preferences")
    op.drop_index("ix_signal_preferences_tenant_id", table_name="signal_preferences")
    op.drop_table("signal_preferences")
