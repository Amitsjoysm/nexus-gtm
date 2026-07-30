"""Platform-admin permission grants.

Additive and behaviour-preserving: the column defaults to an empty list, and
``permissions.effective_permissions`` treats empty as "use the role preset". Every existing
admin therefore keeps exactly the access their role implies, with no backfill and no window in
which someone loses the console.

Revision ID: 0029_admin_permissions
Revises: 0028_user_mfa
Create Date: 2026-07-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_admin_permissions"
down_revision = "0028_user_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_admins",
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("platform_admins", "permissions")
