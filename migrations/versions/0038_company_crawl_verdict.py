"""per-company shared-crawl verdict

Fan-out is enabled globally, but each company still has to earn it. ``diff.py`` compares the shared
crawl against the per-tenant crawl for an account; this column records that answer so fan-out and
the per-tenant skip can both read it.

Both gates MUST test the same condition. If fan-out required a verdict and the per-tenant skip did
not, an account would get neither crawl — signals would simply stop, which looks exactly like a
quiet market and is the failure mode this whole subsystem is built to avoid.

Default ``unknown``, which means "keep crawling per-tenant". A company only moves to ``agrees``
after a real comparison, so turning the global flag on changes nothing until the evidence exists.

Revision ID: 0038_company_crawl_verdict
Revises: 0037_shared_people
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_company_crawl_verdict"
down_revision = "0037_shared_people"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "crawl_verdict", sa.String(length=20), nullable=False, server_default="unknown"
        ),
    )
    op.add_column(
        "companies", sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True)
    )
    # The fan-out sweep's only scan: "which companies have earned delivery?"
    op.create_index("ix_company_verdict", "companies", ["crawl_verdict"])


def downgrade() -> None:
    op.drop_index("ix_company_verdict", table_name="companies")
    op.drop_column("companies", "verdict_at")
    op.drop_column("companies", "crawl_verdict")
