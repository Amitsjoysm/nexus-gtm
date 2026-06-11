"""Performance: composite (tenant_id, time) indexes for the live activity feed.

Revision ID: 0010_perf_indexes
Revises: 0009_crm_auto_sync
Create Date: 2026-06-11

The dashboard polls "newest N rows for this tenant" on four tables. With only the
single-column tenant_id index, each poll sorts the tenant's full history; these composite
indexes turn each read into a bounded index walk.

Note: the offline test path builds schema via Base.metadata.create_all (not alembic upgrade
head), matching migrations 0005-0009. This migration is for Postgres production; it is verified
by the revision-chain assertion in tests/test_perf_hardening.py.
"""
from __future__ import annotations

from alembic import op

revision = "0010_perf_indexes"
down_revision = "0009_crm_auto_sync"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_signal_tenant_occurred", "signal_events", ["tenant_id", "occurred_at"]),
    ("ix_alert_tenant_created", "alerts", ["tenant_id", "created_at"]),
    ("ix_score_tenant_computed", "account_scores", ["tenant_id", "computed_at"]),
    ("ix_agentrun_tenant_created", "agent_runs", ["tenant_id", "created_at"]),
)


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
