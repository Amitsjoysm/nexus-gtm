"""Accounts: dedicated archived_at column (expand step) + backfill from custom_fields.

Gives the accounts list an indexable archived filter so it can paginate in SQL instead of
fetching a page and dropping archived rows in Python (which silently shrank the page). The
legacy ``custom_fields['archived']`` boolean stays mirrored (dual-write in the app) so a
rollback to prior code keeps working; this migration only adds the column and backfills it.

Revision ID: 0020_account_archived_at
Revises: 0019_verification_icp_controls
Create Date: 2026-07-21
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0020_account_archived_at"
down_revision = "0019_verification_icp_controls"
branch_labels = None
depends_on = None


def _is_archived(custom_fields) -> bool:
    """Dialect-safe read of the legacy JSON flag: Postgres returns a dict, SQLite a TEXT string."""
    if custom_fields is None:
        return False
    if isinstance(custom_fields, str):
        try:
            custom_fields = json.loads(custom_fields)
        except (ValueError, TypeError):
            return False
    return bool(isinstance(custom_fields, dict) and custom_fields.get("archived"))


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_index("ix_accounts_archived_at", ["archived_at"])

    # Backfill: rows whose legacy JSON flag says archived get archived_at = created_at (a stable,
    # already-present lower bound — the exact archive time is unknown for historical rows, and the
    # list filter only distinguishes NULL vs non-NULL). Batched, dialect-agnostic Python loop.
    bind = op.get_bind()
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.String),
        sa.column("custom_fields", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("archived_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(accounts.c.id, accounts.c.custom_fields, accounts.c.created_at)
    ).fetchall()
    for row in rows:
        if _is_archived(row.custom_fields):
            bind.execute(
                accounts.update()
                .where(accounts.c.id == row.id)
                .values(archived_at=row.created_at)
            )


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_index("ix_accounts_archived_at")
        batch.drop_column("archived_at")
