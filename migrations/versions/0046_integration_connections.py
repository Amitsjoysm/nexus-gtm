"""Generalize crm_connections into integration_connections (adds a ``kind`` discriminator).

``crm_connections`` was deliberately CRM-only — a YAGNI call against a generic credential table
that was correct until per-tenant SEP credentials arrived. The two rows differ only in which
vendor the token belongs to; everything around them (write-only secret, status ladder,
verified_at/last_error, per-tenant resolution with an env fallback) is identical, so a second
table would have duplicated that entire surface.

Existing rows are CRM by definition, so ``kind`` backfills to 'crm' via the server default and the
rename preserves every row. The uniqueness rule moves from "one connection per tenant" to "one
connection per tenant per kind", which is what lets a tenant hold a CRM and a SEP credential.

Revision ID: 0046_integration_connections
Revises: 0045_audit_log
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_integration_connections"
down_revision = "0045_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("crm_connections", "integration_connections")
    # batch_alter_table so SQLite (which cannot ALTER a constraint in place) rebuilds the table.
    with op.batch_alter_table("integration_connections") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="crm")
        )
        batch.drop_constraint("uq_crm_connection_tenant", type_="unique")
        batch.create_unique_constraint(
            "uq_integration_connection_tenant_kind", ["tenant_id", "kind"]
        )
    op.drop_index("ix_crm_connections_tenant_id", table_name="integration_connections")
    op.create_index(
        "ix_integration_connections_tenant_id", "integration_connections", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_integration_connections_tenant_id", table_name="integration_connections")
    # Only CRM rows fit the old shape; anything else would violate the one-per-tenant unique.
    conns = sa.table("integration_connections", sa.column("kind", sa.String))
    op.get_bind().execute(conns.delete().where(conns.c.kind != "crm"))
    with op.batch_alter_table("integration_connections") as batch:
        batch.drop_constraint("uq_integration_connection_tenant_kind", type_="unique")
        batch.create_unique_constraint("uq_crm_connection_tenant", ["tenant_id"])
        batch.drop_column("kind")
    op.create_index("ix_crm_connections_tenant_id", "integration_connections", ["tenant_id"])
    op.rename_table("integration_connections", "crm_connections")
