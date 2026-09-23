"""signal dates: say whether occurred_at is when it happened or only when we found it

``signal_events.dated`` and ``company_signals.dated``: ``event`` when ``occurred_at`` is the date
the source gave (an RSS pubDate, an SEC filing date, a search provider's publish date, or an
observation of the present like open job counts), ``found`` when it is only the moment we collected
it.

Nullable with no backfill, and NULL reads as ``found`` — which is the truth about every row written
before this: no source set ``occurred_at``, so all of them were dated at collection. Measured on the
local database: 1,099 of 1,112 signals dated within ten minutes of being collected.
``scripts/repair_signal_dates.py`` re-dates what it can prove.

Revision ID: 0057_signal_dated
Revises: 0056_alert_routing
Create Date: 2026-09-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_signal_dated"
down_revision = "0056_alert_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_events", sa.Column("dated", sa.String(length=8), nullable=True))
    op.add_column("company_signals", sa.Column("dated", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("company_signals", "dated")
    op.drop_column("signal_events", "dated")
