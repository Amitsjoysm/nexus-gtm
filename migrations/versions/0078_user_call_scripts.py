"""user call scripts: an SDR's own call script, shown in the call console

Asked by the SDR team (2026-11-04): an SDR uploads their own script from the call console and
reads it during their calls. One row per person per workspace; the text is stored as written.
Tenant-scoped, so ``scripts/apply_rls.py`` enrols it on deploy.

Revision ID: 0078_user_call_scripts
Revises: 0077_icp_services
Create Date: 2026-11-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_user_call_scripts"
down_revision = "0077_icp_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_call_scripts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_call_script"),
    )
    op.create_index("ix_user_call_scripts_tenant_id", "user_call_scripts", ["tenant_id"])
    op.create_index("ix_user_call_scripts_user_id", "user_call_scripts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_call_scripts_user_id", table_name="user_call_scripts")
    op.drop_index("ix_user_call_scripts_tenant_id", table_name="user_call_scripts")
    op.drop_table("user_call_scripts")
