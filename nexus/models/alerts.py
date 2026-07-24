"""Alerts: tenant-scoped notifications raised by plays, agents, or signal thresholds."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
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
