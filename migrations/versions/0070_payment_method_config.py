"""payment credentials: the Stripe payment method configuration Checkout uses

An optional `pmc_...` id per Stripe credential. Empty (every existing row) means the account's
Default Config, which is what Checkout used before. A configuration belongs to ONE Stripe account,
so it lives on the credential rather than in a global setting: switching the active account must
not send another account's id to Stripe. `payment_credentials` is platform-global (no tenant_id),
so RLS does not apply.

Revision ID: 0070_payment_method_config
Revises: 0069_reply_switches
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Was "0070_payment_method_configuration" (33 characters) until 2026-10-14. Postgres keeps the
# revision in alembic_version.version_num, a varchar(32), so recording it failed and the app
# would not start; SQLite does not enforce the length, so no test saw it. No database ever
# recorded the long id (the failure rolls the whole upgrade back), so renaming is safe.
revision = "0070_payment_method_config"
down_revision = "0069_reply_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_credentials", sa.Column(
        "payment_method_configuration", sa.String(length=64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("payment_credentials", "payment_method_configuration")
