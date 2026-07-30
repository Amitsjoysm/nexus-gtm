"""Job durability: dead-lettered background jobs.

Additive. Platform-global — deliberately NO ``tenant_id`` column, because
``scripts/apply_rls.py`` enrols any table having one into Row-Level Security, and both writers
(the worker, running cross-tenant sweeps) and readers (platform operators) reach this table with
no tenant binding. Under RLS they would get zero rows rather than an error. The affected tenant
is recorded as ``subject_tenant_id``, matching ``billing_audit_log`` / ``billing_webhook_events``.

Revision ID: 0027_dead_letter_jobs
Revises: 0026_billing_webhooks
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_dead_letter_jobs"
down_revision = "0026_billing_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("job_name", sa.String(length=80), nullable=False),
        # The exact handler payload, so a replay re-runs the original work verbatim.
        sa.Column("payload", sa.JSON(), nullable=True),
        # sha256(name + payload): a job failing the same way repeatedly updates one row.
        sa.Column("payload_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dead_letter_jobs_job_name", "dead_letter_jobs", ["job_name"])
    op.create_index("ix_dead_letter_jobs_payload_digest", "dead_letter_jobs", ["payload_digest"])
    # The triage query: open dead letters, optionally narrowed to one job name.
    op.create_index("ix_dead_letter_jobs_open", "dead_letter_jobs", ["job_name", "replayed_at"])


def downgrade() -> None:
    op.drop_table("dead_letter_jobs")
