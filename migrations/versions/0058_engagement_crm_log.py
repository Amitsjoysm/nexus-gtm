"""engagement CRM activity log: what has already been written to the workspace's CRM

Two nullable timestamps, one per thing the CRM log writes (spec §19, "CRM activity logging"):

* ``engagement_messages.crm_logged_at`` — a sent email or a received reply, logged once.
* ``reply_classifications.crm_logged_at`` — a meeting booked from the reply desk, logged once. The
  reply the meeting came from is a different activity and carries its own mark on its message.

A mark rather than a per-workspace watermark: a watermark read past a row that committed late would
skip it forever, and one stored in ``tenants.email_settings`` would race an admin saving settings.
NULL for every existing row; the sweep only looks back seven days, so nothing old is backfilled.

Revision ID: 0058_engagement_crm_log
Revises: 0057_engagement
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_engagement_crm_log"
down_revision = "0057_engagement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engagement_messages") as batch:
        batch.add_column(sa.Column("crm_logged_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("reply_classifications") as batch:
        batch.add_column(sa.Column("crm_logged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reply_classifications") as batch:
        batch.drop_column("crm_logged_at")
    with op.batch_alter_table("engagement_messages") as batch:
        batch.drop_column("crm_logged_at")
