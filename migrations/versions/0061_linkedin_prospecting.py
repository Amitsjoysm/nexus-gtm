"""linkedin prospecting: LinkedIn facts on shared companies, search cursors, populate runs

* ``companies`` gains what the company-search actor returns: LinkedIn page and id, industry code,
  the size band, HQ country, description, when it was read, and LinkedIn's similar pages. All
  nullable; every existing row simply lacks them.
* ``company_countries`` — every country a company has an office in. LinkedIn's location filter
  matches any office, not the headquarters, so the database step must match the same way.
  Platform-global, like ``companies``.
* ``prospect_cursors`` — how far through each actor query we have read. Platform-global (no
  ``tenant_id``): the pages land in the shared ``companies`` table, so no workspace should pay to
  read one again.
* ``prospect_runs`` — one workspace's "populate N companies" request and its outcome.
  Tenant-scoped, so ``scripts/apply_rls.py`` enrols it.

Additive only.

Revision ID: 0061_linkedin_prospecting
Revises: 0060_web_cache
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_linkedin_prospecting"
down_revision = "0060_web_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("linkedin_url", sa.String(length=255), nullable=True))
    op.add_column("companies", sa.Column("linkedin_id", sa.String(length=32), nullable=True))
    op.add_column("companies", sa.Column("linkedin_industry_id", sa.Integer(), nullable=True))
    op.add_column("companies", sa.Column("employee_range_min", sa.Integer(), nullable=True))
    op.add_column("companies", sa.Column("employee_range_max", sa.Integer(), nullable=True))
    op.add_column("companies", sa.Column("hq_country_code", sa.String(length=2), nullable=True))
    op.add_column("companies", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("companies", sa.Column("linkedin_fetched_at", sa.DateTime(timezone=True),
                                         nullable=True))
    op.add_column("companies", sa.Column("similar_linkedin", sa.JSON(), nullable=True))
    op.create_index("ix_companies_linkedin_url", "companies", ["linkedin_url"])
    op.create_index("ix_company_linkedin_industry", "companies", ["linkedin_industry_id"])

    op.create_table(
        "company_countries",
        sa.Column("company_id", sa.String(length=40), sa.ForeignKey("companies.id"),
                  primary_key=True),
        sa.Column("country_code", sa.String(length=2), primary_key=True),
    )
    op.create_index("ix_company_countries_country_code", "company_countries", ["country_code"])

    op.create_table(
        "prospect_cursors",
        sa.Column("query_key", sa.String(length=40), primary_key=True),
        sa.Column("query", sa.JSON(), nullable=False),
        sa.Column("next_page", sa.Integer(), nullable=False),
        sa.Column("total_pages", sa.Integer(), nullable=True),
        sa.Column("total_results", sa.Integer(), nullable=True),
        sa.Column("exhausted", sa.Boolean(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "prospect_runs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("requested", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("discarded", sa.JSON(), nullable=False),
        sa.Column("account_ids", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_prospect_runs_tenant_id", "prospect_runs", ["tenant_id"])
    op.create_index("ix_prospect_runs_tenant_created", "prospect_runs", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_prospect_runs_tenant_created", table_name="prospect_runs")
    op.drop_index("ix_prospect_runs_tenant_id", table_name="prospect_runs")
    op.drop_table("prospect_runs")
    op.drop_table("prospect_cursors")
    op.drop_index("ix_company_countries_country_code", table_name="company_countries")
    op.drop_table("company_countries")
    op.drop_index("ix_company_linkedin_industry", table_name="companies")
    op.drop_index("ix_companies_linkedin_url", table_name="companies")
    for col in ("similar_linkedin", "linkedin_fetched_at", "description", "hq_country_code",
                "employee_range_max", "employee_range_min", "linkedin_industry_id", "linkedin_id",
                "linkedin_url"):
        op.drop_column("companies", col)
