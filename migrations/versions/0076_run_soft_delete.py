"""records housekeeping: deleting an AI run, and what deleting accounts leaves behind

* ``orchestration_runs.deleted_at``: a deleted run leaves the AI Runs list and its detail, and the
  row stays. Its usage events are separate rows and are untouched, so billing and the credit report
  still count the run. Nullable, no backfill; the table keeps its RLS policy.
* ``discovery_suppressions``: domains a workspace said "don't suggest these again" about when it
  deleted their accounts. Populate and the daily sweep read it beside the accounts the workspace
  holds. Tenant-scoped, so ``apply_rls.py`` enrols it by its ``tenant_id``.
* ``company_reports``: a workspace saying a shared company's data is wrong. One row per company per
  workspace, so a second workspace's report never overwrites the first. Platform-global like
  ``companies``, with the reporting workspace in ``subject_tenant_id`` so ``apply_rls.py`` leaves it
  alone and the operators who review it can read it. A report never quarantines the row.

Additive only.

Revision ID: 0076_run_soft_delete
Revises: 0075_call_scripts
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076_run_soft_delete"
down_revision = "0075_call_scripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orchestration_runs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "discovery_suppressions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "domain", name="uq_discovery_suppression_domain"),
    )
    op.create_index(
        "ix_discovery_suppressions_tenant_id", "discovery_suppressions", ["tenant_id"]
    )

    op.create_table(
        "company_reports",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("company_id", sa.String(length=40), sa.ForeignKey("companies.id"),
                  nullable=False),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=False),
        sa.Column("reported_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "subject_tenant_id", name="uq_company_report"),
    )
    op.create_index("ix_company_reports_status", "company_reports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_company_reports_status", table_name="company_reports")
    op.drop_table("company_reports")
    op.drop_index("ix_discovery_suppressions_tenant_id", table_name="discovery_suppressions")
    op.drop_table("discovery_suppressions")
    op.drop_column("orchestration_runs", "deleted_at")
