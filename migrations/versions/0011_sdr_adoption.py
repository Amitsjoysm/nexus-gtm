"""SDR adoption: campaign attribution on outcomes.

Revision ID: 0011_sdr_adoption
Revises: 0010_perf_indexes
Create Date: 2026-06-11

Adds outcomes.campaign_id so replies/meetings/wins roll up to the campaign that produced
them (campaign ROI reporting).

Note: the offline test path builds schema via Base.metadata.create_all (not alembic upgrade
head), matching migrations 0005-0010. This migration is for Postgres production; it is verified
by the revision-chain assertion in tests/test_sdr_adoption.py.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_sdr_adoption"
down_revision = "0010_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outcomes",
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_outcomes_campaign", "outcomes", "campaigns", ["campaign_id"], ["id"]
    )
    op.create_index("ix_outcomes_campaign_id", "outcomes", ["campaign_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_outcomes_campaign_id", table_name="outcomes")
    op.drop_constraint("fk_outcomes_campaign", "outcomes", type_="foreignkey")
    op.drop_column("outcomes", "campaign_id")
