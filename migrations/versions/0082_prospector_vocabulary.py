"""prospector: the vocabulary the filters' typeahead offers

``prospector_vocabulary`` is platform-global (no ``tenant_id``), like ``prospector_pages``: values
the provider's own data holds for a text filter (an industry, a title, a city), learned from its
answers, which are the same for every workspace. ``apply_rls.py`` leaves it alone. Only business
facts, never a person's name, email or phone, nor a company's domain or LinkedIn page.

Additive only.

Revision ID: 0082_prospector_vocabulary
Revises: 0081_icp_strict_fit
Create Date: 2026-11-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082_prospector_vocabulary"
down_revision = "0081_icp_strict_fit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospector_vocabulary",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("view", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=24), nullable=False),
        sa.Column("value", sa.String(length=160), nullable=False),
        sa.Column("value_lower", sa.String(length=160), nullable=False),
        sa.Column("seen", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_prospector_vocabulary_lookup", "prospector_vocabulary",
                    ["view", "field", "value_lower"])


def downgrade() -> None:
    op.drop_index("ix_prospector_vocabulary_lookup", table_name="prospector_vocabulary")
    op.drop_table("prospector_vocabulary")
