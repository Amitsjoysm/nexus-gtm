"""reply switches: each SDR's half of "act alone" and "send AI replies alone", and the automatic
answer's state on the reply it answers

All three tables are tenant-scoped and already enrolled in RLS; additive columns only. Existing
mailboxes get today's behaviour: acting alone on, sending alone off.

Revision ID: 0069_reply_switches
Revises: 0068_mcp
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_reply_switches"
down_revision = "0068_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mailbox_connections", sa.Column("auto_actions", sa.Boolean(), nullable=False,
                                                   server_default=sa.true()))
    op.add_column("mailbox_connections", sa.Column("auto_send_replies", sa.Boolean(),
                                                   nullable=False, server_default=sa.false()))
    op.add_column("reply_classifications", sa.Column("auto_reply", sa.String(8), nullable=True))
    op.add_column("reply_classifications", sa.Column("auto_reply_note", sa.Text(), nullable=True))
    op.create_index("ix_reply_classifications_auto_reply", "reply_classifications",
                    ["auto_reply"])


def downgrade() -> None:
    op.drop_index("ix_reply_classifications_auto_reply", table_name="reply_classifications")
    op.drop_column("reply_classifications", "auto_reply_note")
    op.drop_column("reply_classifications", "auto_reply")
    op.drop_column("mailbox_connections", "auto_send_replies")
    op.drop_column("mailbox_connections", "auto_actions")
