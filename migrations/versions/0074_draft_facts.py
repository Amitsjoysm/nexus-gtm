"""draft facts: what an AI draft was given to rest on, shown beside it on Review

``engagement_messages.draft_facts``: the facts a draft was offered, each with its link where it has
one (a signal's article, the company's own page). SDRs asked for drafts built on verified facts and
to see them (2026-10-26, D8). Nullable, no backfill: a draft written before reads as "not
recorded". Additive only; the table keeps its RLS policy.

Revision ID: 0074_draft_facts
Revises: 0073_field_provenance
Create Date: 2026-10-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074_draft_facts"
down_revision = "0073_field_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engagement_messages", sa.Column("draft_facts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("engagement_messages", "draft_facts")
