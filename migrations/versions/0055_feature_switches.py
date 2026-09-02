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


def downgrade() -> None:
    op.drop_table("feature_switches")
