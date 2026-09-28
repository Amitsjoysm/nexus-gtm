"""calling billing: a record of every platform call, and a caller ID per workspace

* ``placed_calls`` — one row per live call placed on the PLATFORM Twilio. A platform call used to be
  charged only when the rep logged its outcome, so a call nobody logged was free. With a row per
  call, a sweep charges any still uncharged two hours on (decided with the product owner
  2026-09-23). Tenant-scoped, so ``scripts/apply_rls.py`` enrols it.
* ``tenants.platform_caller_id`` — the number a workspace's platform calls show, assigned by a
  superadmin. NULL uses the platform's default, which is every workspace's behaviour before this.

Revision ID: 0061_calling_billing
Revises: 0060_integration_config
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_calling_billing"
down_revision = "0060_integration_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "placed_calls",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("call_task_id", sa.String(length=32), nullable=True),
        sa.Column("provider_call_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # One row per call: dialling twice with one idempotency key must not record it twice.
        sa.UniqueConstraint("tenant_id", "provider_call_id", name="uq_placed_call"),
    )
    op.create_index("ix_placed_calls_tenant_id", "placed_calls", ["tenant_id"])
    # The sweep's question: uncharged calls, oldest first.
    op.create_index("ix_placed_calls_uncharged", "placed_calls", ["charged_at", "placed_at"])
    op.add_column("tenants", sa.Column("platform_caller_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "platform_caller_id")
    op.drop_index("ix_placed_calls_uncharged", table_name="placed_calls")
    op.drop_index("ix_placed_calls_tenant_id", table_name="placed_calls")
    op.drop_table("placed_calls")
