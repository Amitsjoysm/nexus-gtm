"""stripe.manage: creating plans and subscriptions at Stripe is superadmin-only

No schema change. `platform_admins.permissions` stores the EXPANDED set, so a new permission
reaches nobody with a stored list: backfill it into rows that held every permission existing
before it, exactly as 0055 did for `features.manage`. A narrowed admin stays narrowed.

Revision ID: 0067_stripe_manage
Revises: 0066_page_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_stripe_manage"
down_revision = "0066_page_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import json

    from nexus.billing.permissions import (
        PERMISSIONS_BEFORE_STRIPE_MANAGE,
        STRIPE_MANAGE,
        widened_with,
    )

    bind = op.get_bind()
    try:
        rows = bind.execute(sa.text("SELECT id, permissions FROM platform_admins")).fetchall()
    except Exception:
        return  # table absent on a partial chain; nothing to backfill
    for row_id, raw in rows:
        held = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        if not isinstance(held, list):
            continue
        widened = widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE)
        if widened is not None:
            bind.execute(
                sa.text("UPDATE platform_admins SET permissions = :p WHERE id = :i"),
                {"p": json.dumps(widened), "i": row_id},
            )


def downgrade() -> None:
    # Nothing to undo in the schema. A granted permission left in a row is harmless on an older
    # release, which simply does not check it.
    pass
