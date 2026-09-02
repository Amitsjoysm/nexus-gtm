"""A platform-wide switch that takes a feature offline, with a message.

Keyed on a `module.*` capability id — the SAME ids the nav items, the `RequireCapability` route
guard and the capability catalog's `depends_on` already use. A switch therefore reaches all three
without any of them changing, and a page added in a later release is covered the moment it is given
a capability, which it needs anyway to be sellable.

The alternative — a separate page registry — would be a fourth source of truth that has to agree
with those three, and the first thing to drift would be which pages it covers.

Platform-global: no ``tenant_id``, so `scripts/apply_rls.py` leaves it alone, exactly like
`billing_feature_flags` and `billing_capabilities`. "Calling is down for maintenance" is a statement
about the platform, not about one customer.

THE ABSENCE OF A ROW MEANS ENABLED. That is what makes the table additive — a deployment with no
switches behaves exactly as it did before it existed.
"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, TimestampMixin

# Four states, not a boolean. `enabled` is the absence of a restriction; the other three all block
# and differ ONLY in what the customer is told — which is the point. "We turned this off", "this is
# not built yet" and "this is broken right now" are three different conversations, and a single
# flag makes a support agent guess which one they are having.
SWITCH_STATES = ("enabled", "disabled", "coming_soon", "maintenance")


class FeatureSwitch(TimestampMixin, Base):
    __tablename__ = "feature_switches"

    # The capability id IS the key. A surrogate id would mean a join on the resolution path, which
    # runs inside every entitlement decision.
    capability_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="enabled", nullable=False)
    # Shown to the customer verbatim. Empty falls back to wording chosen per state in the UI, so a
    # superadmin flipping a switch in a hurry never leaves a blank banner on a customer's screen.
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Who did it. This one takes a feature away from every customer at once, so it is the last
    # mutation that should be untraceable.
    updated_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
