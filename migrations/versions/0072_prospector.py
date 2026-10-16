"""prospector: the shared vendor page cache, the paid ledger, and the daily browse counter

* ``prospector_pages`` is platform-global (no ``tenant_id``), like ``web_cache``: an InfoJoy Search
  answer is the same for every workspace, bought once and reused for 24 hours. Its payload is
  Fernet-sealed PII with an expiry, so ``apply_rls.py`` leaves it alone and the prune sweep empties it.
* ``prospector_purchases`` and ``prospector_daily_rows`` are tenant-scoped, so ``apply_rls.py`` enrols
  them by their ``tenant_id``.

Additive only.

Revision ID: 0072_prospector
Revises: 0071_ledger_erasures
Create Date: 2026-10-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072_prospector"
down_revision = "0071_ledger_erasures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospector_pages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("payload_sealed", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_prospector_pages_expires", "prospector_pages", ["expires_at"])

    op.create_table(
        "prospector_purchases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("contact_id", sa.String(length=32), nullable=True),
        sa.Column("account_id", sa.String(length=32), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("first_charged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "fingerprint", "field", name="uq_prospector_purchase"),
    )
    op.create_index("ix_prospector_purchases_tenant_id", "prospector_purchases", ["tenant_id"])

    op.create_table(
        "prospector_daily_rows",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "day", name="uq_prospector_daily_rows"),
    )
    op.create_index("ix_prospector_daily_rows_tenant_id", "prospector_daily_rows", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_prospector_daily_rows_tenant_id", table_name="prospector_daily_rows")
    op.drop_table("prospector_daily_rows")
    op.drop_index("ix_prospector_purchases_tenant_id", table_name="prospector_purchases")
    op.drop_table("prospector_purchases")
    op.drop_index("ix_prospector_pages_expires", table_name="prospector_pages")
    op.drop_table("prospector_pages")
