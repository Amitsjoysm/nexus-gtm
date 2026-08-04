# nexus/models/notification_preference.py
"""Per-user notification routing.

Channels were configured **per environment** — one webhook URL, one Slack hook, one email address in
`Settings` for the whole deployment. So "users choose where their alerts go" was not expressible at
all: every rep in every workspace got the same routing, or none.

A preference is per *user*, per *category*, per *channel*. The absence of a row is meaningful and is
not a gap: it means "no preference expressed", and the tenant-level configuration continues to apply
exactly as before. That is what makes this additive — nobody's delivery changes until they say so.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# How eagerly a user wants a category delivered.
DELIVERY_MODES = ("immediate", "digest", "off")


class NotificationPreference(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        # One row per user per category per channel. Without this a UI that saves twice produces
        # two contradictory preferences and delivery becomes a coin flip.
        UniqueConstraint(
            "tenant_id", "user_id", "category", "channel", name="uq_notification_preference"
        ),
        Index("ix_notification_pref_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # Matches nexus/alerts/rules.py ALERT_CATEGORIES. Stable strings: renaming one silently
    # unsubscribes everybody who chose it.
    category: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="in_app")
    mode: Mapped[str] = mapped_column(String(20), default="immediate")
    # Quiet hours in the user's local offset, as minutes from midnight. Null disables them.
    # Stored as minutes rather than a time so the "22:00 → 07:00" wrap is arithmetic, not a
    # special case in every comparison.
    quiet_from_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quiet_to_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Minutes to add to UTC to reach the user's local time. Explicit rather than a tz name so
    # evaluation needs no timezone database and cannot fail on an unknown zone.
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    # Watermark for the digest sweep: what has this person already been told? NULL means never,
    # and the first run uses a bounded window rather than replaying the whole alert history.
    last_digest_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # A critical alert ignores quiet hours by default: someone actively evaluating vendors is a
    # short window, and a rep would rather be woken than lose it. Per-user, because that trade is
    # theirs to make.
    quiet_hours_allow_critical: Mapped[bool] = mapped_column(Boolean, default=True)
