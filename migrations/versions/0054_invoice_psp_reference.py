"""billing_invoices.psp_reference / psp_invoice_id — indexed provider lookup

Matching a payment webhook to an invoice used to select EVERY finalized-or-paid invoice
platform-wide and compare `meta["psp_reference"]` in Python, because the reference only ever
existed inside a JSON blob. That is O(total invoices) per event, against a provider that retries
aggressively — the clearest scalability blocker on the money path.

Additive and safe to run while serving:

* Both columns are nullable, so the ADD COLUMN takes no table rewrite and no default backfill.
* The backfill below copies what is already in `meta`; `collection.py` writes both places from
  now on, so the two never diverge.
* `find_invoice_by_provider_reference` still falls back to reading `meta` when the indexed
  lookup misses, which covers any row written between the deploy and this migration.

Revision ID: 0054_invoice_psp_reference
Revises: 0053_user_token_version
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_invoice_psp_reference"
down_revision = "0053_user_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_invoices", sa.Column("psp_reference", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "billing_invoices", sa.Column("psp_invoice_id", sa.String(length=120), nullable=True)
    )
    op.create_index("ix_invoice_psp_reference", "billing_invoices", ["psp_reference"])
    op.create_index("ix_invoice_psp_invoice_id", "billing_invoices", ["psp_invoice_id"])

    # Backfill from the JSON the references have always been mirrored into. Written for both
    # dialects because the test suite replays this chain onto SQLite and production runs
    # Postgres; a Postgres-only statement would make the replay test unrunnable.
    #
    # The WHERE clause selects rows that actually carry a reference, NOT `meta IS NOT NULL`:
    # `meta` is declared nullable=False with a dict default, so that predicate is true for every
    # row and rewrites the whole table — under MVCC a dead tuple per invoice, most of them set
    # NULL to NULL. Every invoice awaiting collection and every zero-total invoice has nothing
    # to copy.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE billing_invoices
               SET psp_reference  = NULLIF(meta->>'psp_reference', ''),
                   psp_invoice_id = NULLIF(meta->>'psp_invoice_id', '')
             WHERE NULLIF(meta->>'psp_reference', '') IS NOT NULL
                OR NULLIF(meta->>'psp_invoice_id', '') IS NOT NULL
            """
        )
    else:
        op.execute(
            """
            UPDATE billing_invoices
               SET psp_reference  = NULLIF(json_extract(meta, '$.psp_reference'), ''),
                   psp_invoice_id = NULLIF(json_extract(meta, '$.psp_invoice_id'), '')
             WHERE NULLIF(json_extract(meta, '$.psp_reference'), '') IS NOT NULL
                OR NULLIF(json_extract(meta, '$.psp_invoice_id'), '') IS NOT NULL
            """
        )


def downgrade() -> None:
    op.drop_index("ix_invoice_psp_invoice_id", table_name="billing_invoices")
    op.drop_index("ix_invoice_psp_reference", table_name="billing_invoices")
    op.drop_column("billing_invoices", "psp_invoice_id")
    op.drop_column("billing_invoices", "psp_reference")
