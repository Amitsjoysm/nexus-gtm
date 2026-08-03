"""Shared company records.

Additive: two new platform-global tables plus a NULLABLE `accounts.company_id`. No existing row is
rewritten and no existing query changes — an account whose `company_id` is null behaves exactly as
it did before.

Neither table carries `tenant_id`, so `scripts/apply_rls.py` correctly leaves them alone. That is
deliberate: enrolling them would make the shared crawler see zero rows, which under RLS is silent
rather than an error.

Revision ID: 0035_companies
Revises: 0034_contact_soft_delete
Create Date: 2026-08-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_companies"
down_revision = "0034_contact_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        # sha1 of the normalised domain — a hash key, so a future hash-partition split is
        # mechanical rather than a redesign.
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("domain", sa.String(length=255), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("tech_stack", sa.JSON(), nullable=True),
        sa.Column("parent_company_id", sa.String(length=40),
                  sa.ForeignKey("companies.id"), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="crawl"),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_companies_domain", "companies", ["domain"])
    op.create_index("ix_companies_parent_company_id", "companies", ["parent_company_id"])
    op.create_index("ix_company_due", "companies", ["last_crawled_at"])

    op.create_table(
        "company_signals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("company_id", sa.String(length=40), sa.ForeignKey("companies.id"),
                  nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_company_signals_company_id", "company_signals", ["company_id"])
    op.create_index("ix_company_signals_kind", "company_signals", ["kind"])
    op.create_index("ix_company_signals_occurred_at", "company_signals", ["occurred_at"])
    op.create_index("ix_company_signal_timeline", "company_signals",
                    ["company_id", "occurred_at"])
    # Idempotent shared crawl: the same event fetched twice must not duplicate.
    op.create_index("ix_company_signal_dedupe", "company_signals",
                    ["company_id", "dedupe_key"], unique=True)

    op.add_column("accounts", sa.Column("company_id", sa.String(length=40), nullable=True))
    op.create_index("ix_accounts_company_id", "accounts", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_accounts_company_id", table_name="accounts")
    op.drop_column("accounts", "company_id")
    op.drop_table("company_signals")
    op.drop_table("companies")
