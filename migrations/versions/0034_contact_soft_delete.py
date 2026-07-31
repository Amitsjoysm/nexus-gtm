"""Soft delete for contacts.

Additive: one nullable column. Accounts already had `archived_at`; contacts had no equivalent, so
"delete a contact" had no safe implementation — the row is referenced by cadence steps, call records
and outreach history, and removing it would orphan all of them.

Every existing contact keeps `deleted_at IS NULL`, so nothing is hidden by this migration.

Revision ID: 0034_contact_soft_delete
Revises: 0033_billing_feature_flags
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_contact_soft_delete"
down_revision = "0033_billing_feature_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_contacts_deleted_at", "contacts", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_contacts_deleted_at", table_name="contacts")
    op.drop_column("contacts", "deleted_at")
