"""Billing: mark usage events as rolled up.

Replaces the clock-comparison watermark used by quota reads with an explicit per-event marker.
Two Python-stamped timestamps can tie on a coarse clock, which silently drops a real event from
the quota count; a NULL/NOT-NULL marker cannot tie.

Additive and safe on a live database: the column is nullable with no default, so the ADD COLUMN
is metadata-only, and every pre-existing row starts as "not yet rolled up" — which is the
conservative direction (such rows are summed live rather than assumed already counted).

Revision ID: 0023_billing_rollup_marker
Revises: 0022_billing_usage
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_billing_rollup_marker"
down_revision = "0022_billing_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_usage_events",
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The quota hot path reads exactly this predicate (tenant + capability + rolled_at IS NULL).
    op.create_index(
        "ix_usage_unrolled",
        "billing_usage_events",
        ["tenant_id", "capability_id", "rolled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_unrolled", table_name="billing_usage_events")
    op.drop_column("billing_usage_events", "rolled_at")
