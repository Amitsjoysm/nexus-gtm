"""feature_switches — a superadmin can take a feature offline, platform-wide, with a message

Keyed on the `module.*` capability id, which is already the key for the nav item, the
`RequireCapability` route guard and the endpoint gating in the capability catalog. So the switch
reaches all three enforcement points without any of them changing, and a page shipped later is
covered the moment it is given a capability — which it needs anyway to be sellable.

**No `tenant_id`, so `scripts/apply_rls.py` leaves it alone**, exactly like `billing_capabilities`
and `platform_admins`. "Calling is down for maintenance" is a statement about the platform, not
about one customer, and enrolling it in RLS would make the entitlement engine read zero rows —
silently, since a cross-tenant read under the app role returns no rows rather than raising. That
would fail open, which is the correct direction, but it would mean the feature never worked and
nothing said so.

Purely additive: an empty table means every feature is enabled, which is exactly how the product
behaved before this existed.

Revision ID: 0055_feature_switches
Revises: 0054_invoice_psp_reference
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_feature_switches"
down_revision = "0054_invoice_psp_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_switches",
        # The capability id IS the primary key. A surrogate would mean a lookup by a non-unique
        # column on a path that runs inside every entitlement decision.
        sa.Column("capability_id", sa.String(length=80), primary_key=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="enabled"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    # No CHECK constraint on `state` on purpose. The resolver already treats an unrecognised state
    # as `enabled` (fail open), and a database-level constraint would turn a rolling deploy — new
    # release writes a state the old one does not know — into a write failure instead of a value
    # the old release safely ignores.

    _grant_features_manage_to_full_superadmins()


def _grant_features_manage_to_full_superadmins() -> None:
    """Give `features.manage` to stored admins who already held every other permission.

    `platform_admins.permissions` stores the EXPANDED set rather than the role name, deliberately:
    redefining "support" tomorrow must not silently re-grant power to people provisioned today. The
    cost of that rule is that a NEW permission reaches nobody who has a stored list — so the
    superadmin who deploys this release cannot open the console it adds, and on a deployment where
    they are the only admin that is a lockout with no in-product fix.

    Widening every `platform_role == "superadmin"` row would be the easy answer and is wrong: the
    grant endpoint accepts an explicit `permissions` list alongside the role, so a deliberately
    NARROWED superadmin can exist, and this would silently re-grant it — the exact thing the
    expanded-set rule was written to prevent.

    So the test is on the permissions, not the role: a row that held every permission that existed
    before this one meant "everything" at the time it was written, and a permission added later
    belongs to it. A row missing even one stays exactly as it is.

    Rows with an EMPTY list are untouched and need nothing — `effective_permissions` falls back to
    the role preset for those, which now contains the new permission. The env allowlist is
    unaffected for the same reason it always was: it carries full power by construction.
    """
    import json

    from nexus.billing.permissions import (
        FEATURES_MANAGE,
        PERMISSIONS_BEFORE_FEATURES_MANAGE,
    )

    bind = op.get_bind()
    try:
        rows = bind.execute(
            sa.text("SELECT id, permissions FROM platform_admins")
        ).fetchall()
    except Exception:
        return  # table absent on a partial chain; nothing to backfill

    had_everything = set(PERMISSIONS_BEFORE_FEATURES_MANAGE)
    for row_id, raw in rows:
        if not raw:
            continue                       # empty -> resolves through the role preset
        held = raw if isinstance(raw, list) else json.loads(raw)
        if not isinstance(held, list) or FEATURES_MANAGE in held:
            continue
        if not had_everything.issubset(set(held)):
            continue                       # a narrowed admin stays narrowed
        bind.execute(
            sa.text("UPDATE platform_admins SET permissions = :p WHERE id = :i"),
            {"p": json.dumps(sorted(set(held) | {FEATURES_MANAGE})), "i": row_id},
        )


def downgrade() -> None:
    op.drop_table("feature_switches")
