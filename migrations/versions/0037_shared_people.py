"""shared people store

One row per real human, shared across tenants, so forty workspaces tracking the same VP Engineering
pay for one phone lookup instead of forty.

Platform-global on purpose: **no ``tenant_id``**, so ``scripts/apply_rls.py`` leaves both tables
alone. Enrolling them would return zero rows to the shared resolver — silent under RLS, not an
error — exactly like ``companies`` and ``company_signals``.

Email and phone are stored Fernet-sealed with a separate ``sha256`` column for lookup. Ciphertext
cannot be indexed (Fernet is randomised, so one value seals differently every time), which is why
both columns exist rather than one.

Revision ID: 0037_shared_people
Revises: 0036_proration_adjustments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_shared_people"
down_revision = "0036_proration_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people",
        # sha1 of the resolution key (LinkedIn URL, else normalised email). Deterministic, so two
        # workers racing on one person produce the same key instead of splitting a human in two.
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("company_domain", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("linkedin_url", sa.String(length=400), nullable=True),
        # Hash to find, sealed value to read.
        sa.Column("email_hash", sa.String(length=64), nullable=True),
        sa.Column("email_encrypted", sa.String(length=500), nullable=True),
        sa.Column("phone_hash", sa.String(length=64), nullable=True),
        sa.Column("phone_encrypted", sa.String(length=500), nullable=True),
        sa.Column("phone_raw_encrypted", sa.String(length=500), nullable=True),
        # A recorded `not_found` is what stops the same empty paid lookup being re-purchased.
        sa.Column("phone_status", sa.String(length=20), nullable=False,
                  server_default="unattempted"),
        sa.Column("email_status", sa.String(length=20), nullable=True),
        sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrichment_source", sa.String(length=60), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False,
                  server_default="contact_backfill"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_person_email_hash", "people", ["email_hash"])
    op.create_index("ix_person_linkedin", "people", ["linkedin_url"])
    op.create_index("ix_person_enrich_due", "people", ["last_enriched_at"])
    op.create_index(op.f("ix_people_company_domain"), "people", ["company_domain"])
    op.create_index(op.f("ix_people_phone_status"), "people", ["phone_status"])

    op.create_table(
        "person_identities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("person_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        # Only the hash. The value itself lives sealed on `people`; resolving never needs it back.
        sa.Column("value_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One hash resolves to exactly one person, enforced by the database — a race that tries to
        # attach one address to two people fails loudly instead of silently forking a human.
        sa.UniqueConstraint("kind", "value_hash", name="uq_person_identity_value"),
    )
    op.create_index("ix_person_identity_person", "person_identities", ["person_id"])
    op.create_index(
        op.f("ix_person_identities_person_id"), "person_identities", ["person_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_person_identities_person_id"), table_name="person_identities")
    op.drop_index("ix_person_identity_person", table_name="person_identities")
    op.drop_table("person_identities")
    op.drop_index(op.f("ix_people_phone_status"), table_name="people")
    op.drop_index(op.f("ix_people_company_domain"), table_name="people")
    op.drop_index("ix_person_enrich_due", table_name="people")
    op.drop_index("ix_person_linkedin", table_name="people")
    op.drop_index("ix_person_email_hash", table_name="people")
    op.drop_table("people")
