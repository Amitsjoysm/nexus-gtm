"""Contact email-verification verdict (triage-grade inbox deliverability).

Revision ID: 0003_contact_email_status
Revises: 0002_conversational_orchestrator
Create Date: 2026-06-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_contact_email_status"
down_revision = "0002_conversational_orchestrator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts", sa.Column("email_status", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("contacts", "email_status")
