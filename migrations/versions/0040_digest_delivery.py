"""per-user digest delivery state

``routing.py`` already decides that an alert should be held for a digest — either because the user
chose ``mode="digest"``, or because it arrived inside their quiet hours. Nothing ever sent it. The
alert existed, the decision was recorded, and the notification simply never arrived: the same shape
as the pre-M21 bug where ``signal.created`` had no subscriber.

``last_digest_at`` is the idempotency gate. Without it the sweep either re-sends everything on every
tick or needs a separate delivery-log table; a per-preference watermark makes "what has this person
already been told?" a single indexed read.

Nullable on purpose. NULL means "never sent", and the first sweep uses the digest interval as its
window rather than replaying the whole alert history at somebody on their first morning.

Revision ID: 0040_digest_delivery
Revises: 0039_user_suspension
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_digest_delivery"
down_revision = "0039_user_suspension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The sweep's only scan: "whose digest is due?"
    op.create_index(
        "ix_notif_pref_digest_due",
        "notification_preferences",
        ["mode", "last_digest_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notif_pref_digest_due", table_name="notification_preferences")
    op.drop_column("notification_preferences", "last_digest_at")
