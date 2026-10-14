"""ledger erasures: who has been erased from the training ledger, so they are never collected again

One row per person erased, holding keys and the last run's report, never the address. The shipper
and the dataset builder skip any event that names an erased person, so an erasure holds after an
event is recorded later, and after a rebuild. Platform-global (no ``tenant_id``): an erasure covers
every workspace at once and its readers work across workspaces, so ``apply_rls.py`` leaves it alone.

Revision ID: 0071_ledger_erasures
Revises: 0070_payment_method_configuration
Create Date: 2026-10-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071_ledger_erasures"
down_revision = "0070_payment_method_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_erasures",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("erasure_key", sa.String(length=64), nullable=False),
        sa.Column("person_key", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("requested_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("uq_ledger_erasure_key", "ledger_erasures", ["erasure_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_ledger_erasure_key", table_name="ledger_erasures")
    op.drop_table("ledger_erasures")
