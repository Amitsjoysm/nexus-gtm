"""mailbox style: each mailbox's own writing style, over the workspace's

Asked by the SDR team (2026-11-04): signatures and style examples set per mailbox. The signature
already lives on the mailbox; this adds its tone, length, first ask, P.S. and sample emails. NULL,
or a field left out, uses the workspace's (`email_style.merged_settings`), so nothing changes for
a mailbox that sets none.

Revision ID: 0079_mailbox_style
Revises: 0078_user_call_scripts
Create Date: 2026-11-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_mailbox_style"
down_revision = "0078_user_call_scripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mailbox_connections", sa.Column("style", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("mailbox_connections", "style")
