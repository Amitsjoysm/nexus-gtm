"""Payment-provider credentials, managed from the Control plane.

Deliberately its own table rather than a row in `provider_keys`. That catalog excluded Stripe on
the grounds that *money fails silently* — a wrong key stops checkout and invoicing without erroring
anywhere a person would look — so this carries a rule the generic key pool does not: a credential
set cannot be activated until a live call against it has succeeded, and the account it belongs to
is read back from the provider and stored, because "wrong Stripe account" is the failure that
produces a real charge against the wrong business.

No rotation pool: there is no riding out a bad Stripe key by trying the next one. Exactly one
credential is active at a time.

No ``tenant_id``: deployment-wide, like `provider_keys` and `provider_settings`.

Revision ID: 0046_payment_credentials
Revises: 0045_provider_settings
Create Date: 2026-08-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_payment_credentials"
down_revision = "0045_provider_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_credentials",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="stripe"),
        sa.Column("label", sa.String(120), nullable=False, server_default=""),
        sa.Column("secret_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("publishable_key", sa.String(200), nullable=False, server_default=""),
        sa.Column("key_hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("account_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("account_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="registered"),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_payment_credentials_provider", "payment_credentials", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_payment_credentials_provider", table_name="payment_credentials")
    op.drop_table("payment_credentials")
