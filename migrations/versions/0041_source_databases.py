"""Registered external source databases.

Purely additive: one new platform-global table. Nothing existing reads it, so an upgrade changes
no behaviour anywhere — the table sits empty until a superadmin registers a source, and a source
is not consumable until a dry run has moved it to `verified`.

No `tenant_id`, so `scripts/apply_rls.py` correctly leaves it alone — same rule as `companies` and
`people`. Enrolling it would hide every source from the platform code that must read it, silently,
as zero rows.

`enabled` defaults to FALSE rather than TRUE. A source that starts switched on would be readable
the instant it reached `verified`, which removes the operator's chance to look at the dry-run
output before anything consumes it — and the dry run is the whole safety story for this subsystem.

Revision ID: 0041_source_databases
Revises: 0040_digest_delivery
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_source_databases"
down_revision = "0040_digest_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_databases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="postgres"),
        # Fernet ciphertext; never selected into a response. Text, because Fernet output grows
        # with the plaintext and a DSN has no useful upper bound.
        sa.Column("dsn_encrypted", sa.Text(), nullable=False, server_default=""),
        # Credentials stripped, computed at registration so the console can list sources without
        # unsealing anything.
        sa.Column("dsn_redacted", sa.String(length=400), nullable=False, server_default=""),
        # registered -> connected -> introspected -> mapped -> verified (| failed)
        sa.Column("status", sa.String(length=20), nullable=False, server_default="registered"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("discovered_schema", sa.JSON(), nullable=True),
        sa.Column("mapping", sa.JSON(), nullable=True),
        sa.Column("dry_run", sa.JSON(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The provider's only scan: "which sources may I read from?"
    op.create_index("ix_source_db_usable", "source_databases", ["status", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_source_db_usable", table_name="source_databases")
    op.drop_table("source_databases")
