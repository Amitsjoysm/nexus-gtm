"""icp services: a full ICP per service a company sells, and fit scored per service

Owner decision 2026-10-29, superseding one ICP per workspace:

* ``icp_services``: each further service, with its own ICP, value props, product context, the page
  it was drafted from and its own daily discovery number. The workspace's ``relevance_profiles`` row
  stays the DEFAULT service (``relevance_profiles.name`` names it), so a workspace that adds none
  behaves exactly as before.
* ``account_service_fits``: one row per (account, service), the account's ICP fit against that
  service. Not a column on ``account_scores``, whose newest row is read as the account's score.
* ``accounts.service_id``: the service whose Populate or daily discovery found the account.
* ``engagement_campaigns.service_id``: the one service a campaign pitches.
* ``prospect_runs.service_id``: so each service's daily tally and latest Populate are its own.

All nullable, NULL meaning the default service; no backfill. Both new tables carry ``tenant_id``,
so ``scripts/apply_rls.py`` enrols them on deploy. Branched from ``0075_call_scripts`` beside
``0076_run_soft_delete``; re-parented onto it when the branches merge.

Revision ID: 0077_icp_services
Revises: 0075_call_scripts
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077_icp_services"
down_revision = "0075_call_scripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "icp_services",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("icp", sa.JSON(), nullable=False),
        sa.Column("value_props", sa.JSON(), nullable=False),
        sa.Column("product_context", sa.Text(), nullable=False),
        sa.Column("daily_target", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_icp_services_tenant_id", "icp_services", ["tenant_id"])
    op.create_index("ix_icp_services_archived_at", "icp_services", ["archived_at"])

    op.create_table(
        "account_service_fits",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=False),
        sa.Column("service_id", sa.String(length=32), sa.ForeignKey("icp_services.id"),
                  nullable=False),
        sa.Column("icp_fit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gate", sa.JSON(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "service_id", name="uq_account_service_fit"),
    )
    op.create_index("ix_account_service_fits_tenant_id", "account_service_fits", ["tenant_id"])
    op.create_index("ix_account_service_fits_account_id", "account_service_fits",
                    ["account_id"])
    op.create_index("ix_account_service_fit_tenant_service", "account_service_fits",
                    ["tenant_id", "service_id"])

    op.add_column("relevance_profiles", sa.Column("name", sa.String(length=120), nullable=True))
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("service_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key("fk_accounts_service_id", "icp_services", ["service_id"], ["id"])
    op.create_index("ix_accounts_service_id", "accounts", ["service_id"])
    with op.batch_alter_table("engagement_campaigns") as batch:
        batch.add_column(sa.Column("service_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key("fk_engagement_campaigns_service_id", "icp_services",
                                 ["service_id"], ["id"])
    op.add_column("prospect_runs", sa.Column("service_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("prospect_runs", "service_id")
    with op.batch_alter_table("engagement_campaigns") as batch:
        batch.drop_constraint("fk_engagement_campaigns_service_id", type_="foreignkey")
        batch.drop_column("service_id")
    op.drop_index("ix_accounts_service_id", table_name="accounts")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("fk_accounts_service_id", type_="foreignkey")
        batch.drop_column("service_id")
    op.drop_column("relevance_profiles", "name")
    op.drop_index("ix_account_service_fit_tenant_service", table_name="account_service_fits")
    op.drop_index("ix_account_service_fits_account_id", table_name="account_service_fits")
    op.drop_index("ix_account_service_fits_tenant_id", table_name="account_service_fits")
    op.drop_table("account_service_fits")
    op.drop_index("ix_icp_services_archived_at", table_name="icp_services")
    op.drop_index("ix_icp_services_tenant_id", table_name="icp_services")
    op.drop_table("icp_services")
