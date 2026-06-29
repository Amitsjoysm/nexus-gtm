"""Performance: indexes for latest-score-per-account and the paginated Contacts list.

Revision ID: 0015_scale_indexes
Revises: 0014_icp_auto_discovery
Create Date: 2026-06-22

Two read paths that grow with the workspace:
  * The Accounts list / detail / Fit column resolves the *latest* composite score per account.
    A (tenant_id, account_id, computed_at) index turns that window read into a bounded walk so
    it stays O(accounts) instead of scanning each account's full score history (which grows
    every tick under continuous automation).
  * The workspace Contacts list pages newest-first within a tenant. A (tenant_id, created_at)
    index serves that ORDER BY ... LIMIT without sorting the tenant's whole contact book.

Note: the offline test path builds schema via Base.metadata.create_all (the indexes are declared
on the models), not `alembic upgrade head`; this migration is for Postgres production.
"""
from __future__ import annotations

from alembic import op

revision = "0015_scale_indexes"
down_revision = "0014_icp_auto_discovery"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_score_tenant_account_computed", "account_scores", ["tenant_id", "account_id", "computed_at"]),
    ("ix_contact_tenant_created", "contacts", ["tenant_id", "created_at"]),
)


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
