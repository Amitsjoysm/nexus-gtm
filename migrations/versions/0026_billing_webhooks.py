"""Billing: processed payment-provider webhook events.

Additive. Platform-global — events arrive before we know which tenant they concern, and the id
space belongs to the provider, so there is no ``tenant_id`` column and no RLS policy.

Revision ID: 0026_billing_webhooks
Revises: 0025_billing_audit
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_billing_webhooks"
down_revision = "0025_billing_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_webhook_events",
        # The provider's own event id. Primary key, so replay protection is a database
        # constraint rather than an application check that could be raced.
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="stripe"),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processed"),
        sa.Column("subject_tenant_id", sa.String(length=32), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_billing_webhook_events_event_type", "billing_webhook_events", ["event_type"]
    )
    op.create_index(
        "ix_billing_webhook_type_time", "billing_webhook_events", ["event_type", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("billing_webhook_events")
