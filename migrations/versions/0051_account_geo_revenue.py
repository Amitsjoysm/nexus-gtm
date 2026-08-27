"""Sub-country geography and revenue on accounts.

A tester evaluating the product could filter an ICP on Country and nothing finer, and had no
revenue filter at all — both table stakes for territory-based GTM. An ICP cannot filter on a field
the account record does not carry, so the columns come first.

Additive and nullable, so every existing row is untouched and every query that does not name these
columns is unaffected. `scripts/apply_rls.py` needs no change: `accounts` is already enrolled.

A NULL means "not known", NOT "does not match" — `RelevanceEngine` scores an unknown neutral, the
same rule that keeps an account with no fetched tech stack above the discovery gate.

Revision ID: 0051_account_geo_revenue
Revises: 0050_runtime_settings
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_account_geo_revenue"
down_revision = "0050_runtime_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("region", sa.String(120), nullable=True))
    op.add_column("accounts", sa.Column("postal_code", sa.String(20), nullable=True))
    # BigInteger: revenue in whole currency units passes the 32-bit ceiling at $2.1bn, which is an
    # ordinary enterprise account rather than an edge case.
    op.add_column("accounts", sa.Column("annual_revenue", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "annual_revenue")
    op.drop_column("accounts", "postal_code")
    op.drop_column("accounts", "region")
