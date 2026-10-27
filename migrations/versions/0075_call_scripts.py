"""call scripts: a campaign's own script, uploaded and read out as written

``engagement_campaigns.call_script``: the text of the script the team wrote, kept verbatim and
shown in the call console for that campaign's calls (D10, 2026-10-26). ``call_script_meta``: the
file it came from, who uploaded it and when, an Enhance suggestion waiting to be accepted, and the
original once a suggestion replaced it. Nullable, no backfill; the table keeps its RLS policy.

Revision ID: 0075_call_scripts
Revises: 0074_draft_facts
Create Date: 2026-10-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075_call_scripts"
down_revision = "0074_draft_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engagement_campaigns", sa.Column("call_script", sa.Text(), nullable=True))
    op.add_column("engagement_campaigns", sa.Column("call_script_meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("engagement_campaigns", "call_script_meta")
    op.drop_column("engagement_campaigns", "call_script")
