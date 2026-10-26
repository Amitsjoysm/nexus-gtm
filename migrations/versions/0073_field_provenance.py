"""field provenance: where each value came from, and stamped shared labels held back

* ``accounts.field_sources``, ``contacts.field_sources`` and ``companies.field_sources``: per field,
  the source that wrote it, an evidence reference (a page, a profile, a record) and when. SDRs asked
  to see the source of every account and contact so they can check it; the one column there was
  (``accounts.source``, ``contacts.enrichment_source``) said how the ROW arrived, and the email
  finder overwrote the contact's.
* ``companies.labels_quarantined_at``: set on a shared row whose industry and country were the
  ICP's own words stamped onto a web find (2026-10-26). The chain's shared step does not offer it
  to another workspace until a LinkedIn read labels it.

All nullable, no backfill: NULL reads as "not recorded" and "not quarantined". Additive only;
``accounts`` and ``contacts`` keep their existing RLS policies, ``companies`` stays platform-global.

Revision ID: 0073_field_provenance
Revises: 0072_prospector
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073_field_provenance"
down_revision = "0072_prospector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("field_sources", sa.JSON(), nullable=True))
    op.add_column("contacts", sa.Column("field_sources", sa.JSON(), nullable=True))
    op.add_column("companies", sa.Column("field_sources", sa.JSON(), nullable=True))
    op.add_column("companies", sa.Column("labels_quarantined_at", sa.DateTime(timezone=True),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "labels_quarantined_at")
    op.drop_column("companies", "field_sources")
    op.drop_column("contacts", "field_sources")
    op.drop_column("accounts", "field_sources")
