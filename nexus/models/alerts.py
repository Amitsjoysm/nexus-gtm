"""Alerts: tenant-scoped notifications raised by plays, agents, or signal thresholds."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

ALERT_SEVERITIES = ("info", "warning", "critical")
ALERT_CHANNELS = ("in_app", "webhook", "slack", "email", "telegram")
ALERT_STATUSES = ("open", "acked")


class Alert(IdMixin, TimestampMixin, TenantScoped, Base):
    """A notification delivered in-app and (optionally) fanned out to webhook/email."""

    __tablename__ = "alerts"
    # Backs the activity feed's "newest alerts for this tenant" read.
    __table_args__ = (Index("ix_alert_tenant_created", "tenant_id", "created_at"),)

    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="info", index=True)
    channel: Mapped[str] = mapped_column(String(20), default="in_app")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True, nullable=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signal_events.id"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(40), default="system")  # play|agent|system
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class AlertChannelRule(IdMixin, TimestampMixin, TenantScoped, Base):
    """A workspace rule: "this shared channel receives this category of alert, for everyone".

    The team-level half of routing. A personal route is one member's choice and their own `off` and
    quiet hours apply to it; a rule is a manager's decision about a SHARED channel, and nobody's
    personal setting mutes it — a rep silencing funding for themselves must not silence the team's
    Slack. Rules are also how alerts on UNOWNED accounts reach a channel, since a personal route
    scoped to "my accounts" never covers them.

    Its own table rather than a column on the channel's `IntegrationConnection` row: that row is a
    credential, disconnecting deletes it, and the rules a manager chose should survive swapping one
    Slack webhook for another.
    """

    __tablename__ = "alert_channel_rules"
    __table_args__ = (
        # One row per (channel, category). A UI that saves twice must not produce two rules and
        # post the same alert twice.
        UniqueConstraint("tenant_id", "channel", "category", name="uq_alert_channel_rule"),
    )

    channel: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(40))
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
