"""alert routing: an account owner, a scope on personal routes, and workspace channel rules

Three additive changes that let an alert reach the people who asked for it:

* ``accounts.owner_user_id`` — who works the account. NULL is unowned, not "everyone's". Every
  existing account starts unowned: nobody's choice can be inferred for rows created before ownership
  existed, and guessing (say, from who imported it) would route alerts on a guess.
* ``notification_preferences.scope`` — ``all`` of a category, or only accounts the member owns
  (``mine``). Server default ``all``, because that is what every row written before this column
  MEANT: a saved "funding -> Teams" was never scoped to anything, and narrowing it on upgrade would
  silently stop alerts somebody explicitly asked for.
* ``alert_channel_rules`` — a manager's rule that a shared channel receives a category for everyone.
  Tenant-scoped, so ``scripts/apply_rls.py`` enrols it. Empty for every workspace, and no rule means
  nothing extra is posted, which is exactly the behaviour before this existed.

Revision ID: 0056_alert_routing
Revises: 0055_feature_switches
Create Date: 2026-09-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_alert_routing"
down_revision = "0055_feature_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch mode because SQLite cannot add a foreign key in place, so the replayed chain would stop
    # here; Alembic rebuilds the table there and emits the ordinary ALTER TABLE on Postgres.
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            "fk_accounts_owner_user_id", "users", ["owner_user_id"], ["id"]
        )
    op.create_index("ix_accounts_owner_user_id", "accounts", ["owner_user_id"])

    op.add_column(
        "notification_preferences",
        sa.Column("scope", sa.String(length=10), nullable=False, server_default="all"),
    )

    op.create_table(
        "alert_channel_rules",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # One row per (channel, category): a form that saves twice must not post an alert twice.
        sa.UniqueConstraint("tenant_id", "channel", "category", name="uq_alert_channel_rule"),
    )
    op.create_index("ix_alert_channel_rules_tenant_id", "alert_channel_rules", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_alert_channel_rules_tenant_id", table_name="alert_channel_rules")
    op.drop_table("alert_channel_rules")
    # batch mode so SQLite, which cannot drop a column or a constraint in place, rebuilds the table.
    with op.batch_alter_table("notification_preferences") as batch:
        batch.drop_column("scope")
    op.drop_index("ix_accounts_owner_user_id", table_name="accounts")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("fk_accounts_owner_user_id", type_="foreignkey")
        batch.drop_column("owner_user_id")
