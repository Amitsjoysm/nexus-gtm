"""integration connections: a non-secret config column (first use: a workspace's Twilio caller ID)

``integration_connections.secret`` is sealed and write-only by design — nothing in it may reach a
response. A Twilio connection also has a value that is NOT secret and that the screen must show: the
caller ID every rep's calls display. Storing it in the sealed bundle would mean unsealing inside the
response builder, the one place that keeps "the secret never leaves the server" checkable.

Nullable, no backfill: no existing kind uses it, and NULL reads as an empty config.

Revision ID: 0058_integration_config
Revises: 0057_signal_dated
Create Date: 2026-09-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_integration_config"
down_revision = "0057_signal_dated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_connections", sa.Column("config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("integration_connections", "config")
