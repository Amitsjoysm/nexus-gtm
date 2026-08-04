"""user suspension

An admin could delete a user or leave them fully active, and nothing in between. Someone leaving on
Friday, a compromised account, a contractor between engagements — all three had to be handled by
deletion, which destroys the audit trail of what that person did and orphans the accounts they own.

Suspension is on ``users``, not ``memberships``, deliberately: a compromised account must stop being
able to log in **anywhere**, and a per-membership flag would leave the other workspaces reachable.
Removing someone from one workspace is what deleting a membership already does.

``suspended_at`` rather than a boolean so "when" is answerable without reading the audit log, and
``suspended_reason`` so the next admin does not have to guess.

Revision ID: 0039_user_suspension
Revises: 0038_company_crawl_verdict
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_user_suspension"
down_revision = "0038_company_crawl_verdict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("suspended_reason", sa.String(length=300), nullable=True)
    )
    op.add_column("users", sa.Column("suspended_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "suspended_by")
    op.drop_column("users", "suspended_reason")
    op.drop_column("users", "suspended_at")
