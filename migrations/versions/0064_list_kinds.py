"""prospect lists: a kind (account or contact) and an archive time

Lists could only be built from a filter over accounts, and could not be edited or removed. They now
hold either companies or people, chosen by hand, and are archived rather than deleted: the old
``campaigns.list_id`` is NOT NULL and ``engagement_campaigns.source_list_id`` also points at the row.

``kind`` defaults to ``account`` on the server, which is what every existing list is. ``archived_at``
is nullable and NULL means live, so no backfill.

Revision ID: 0064_list_kinds
Revises: 0063_linkedin_prospecting
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_list_kinds"
down_revision = "0063_linkedin_prospecting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospect_lists", sa.Column(
        "kind", sa.String(length=10), nullable=False, server_default="account"))
    op.add_column("prospect_lists", sa.Column(
        "archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("prospect_lists", "archived_at")
    op.drop_column("prospect_lists", "kind")
