"""Tiered account refresh: store when an account is next due, instead of deriving it.

Additive. `accounts.next_refresh_at` is NOT NULL, backfilled from `last_refreshed_at` so every
existing account keeps exactly the schedule it had — an account refreshed an hour ago stays due in
five hours, and one that has never been refreshed is due now.

**Why store it rather than derive it.** The claim query was
`WHERE last_refreshed_at IS NULL OR last_refreshed_at <= cutoff ORDER BY last_refreshed_at ASC
NULLS FIRST`. No btree can serve that: `ix_accounts_last_refreshed_at` orders NULLS LAST, and the
`OR ... IS NULL` defeats a range scan anyway. Measured on 500k accounts it seq-scanned the table
and sorted 261k rows through a **26 MB external merge on disk** to return 100 — 489 ms warm,
4.58 s cold, every tick, and growing with the estate. Against `next_refresh_at` the same claim is
an index scan that stops at the limit: **44 ms**, and O(batch) rather than O(estate).

NOT NULL with a server default of `now()` is deliberate. A nullable column would reintroduce the
`NULLS FIRST` ordering that made the old index useless, and it would mean a new account's due-ness
depended on a NULL check again. New accounts are due immediately, which is what they were before.

`last_refreshed_at` is untouched and still written — `pipeline.process_account` reads
`last_refreshed_at IS NULL` to decide whether to seed an account from the shared company crawl, and
it remains the honest answer to "when did we last look at this account".

Revision ID: 0042_account_next_refresh
Revises: 0041_source_databases
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_account_next_refresh"
down_revision = "0041_source_databases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable first, backfilled, then made NOT NULL. Adding it NOT NULL outright would
    # stamp every existing account with `now()`, making the entire estate due at the same instant
    # on the deploy — a thundering herd against every third-party source we crawl.
    op.add_column(
        "accounts", sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Preserve each account's existing position in the cycle. 21600s is the 6h hot interval, which
    # is what every account was on before tiering; the pipeline re-tiers each one the next time it
    # runs, so cold accounts drift out to their longer interval naturally rather than all at once.
    #
    # Date arithmetic is the one thing SQL dialects never agree on, and this chain has to replay on
    # SQLite (tests/test_migrations_replay.py) as well as run on Postgres. Written out per dialect
    # rather than reached for via a helper, because a silently-wrong backfill here would leave the
    # whole estate due at once on the deploy.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE accounts SET next_refresh_at = "
            "COALESCE(last_refreshed_at + INTERVAL '21600 seconds', CURRENT_TIMESTAMP)"
        )
    else:
        op.execute(
            "UPDATE accounts SET next_refresh_at = "
            "COALESCE(datetime(last_refreshed_at, '+21600 seconds'), CURRENT_TIMESTAMP)"
        )
    # `batch_alter_table` rather than a plain `alter_column`: SQLite has no
    # `ALTER COLUMN ... SET NOT NULL` and Alembic emulates it by rebuilding the table. On Postgres
    # this compiles to the ordinary ALTER. The chain must replay on both.
    with op.batch_alter_table("accounts") as batch:
        batch.alter_column(
            "next_refresh_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        )
    # The claim's only scan.
    op.create_index("ix_accounts_next_refresh_at", "accounts", ["next_refresh_at"])


def downgrade() -> None:
    op.drop_index("ix_accounts_next_refresh_at", table_name="accounts")
    op.drop_column("accounts", "next_refresh_at")
