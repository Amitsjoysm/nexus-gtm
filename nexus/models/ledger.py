"""Training & insights ledger: consent decisions and the transactional outbox (spec §18).

``training_consents`` is append-only: one row per decision, and the newest row is the one in
force. Keeping every decision rather than a flag on the tenant answers "when did this workspace
agree, to which terms, and who switched it off" without reading the audit log.

``ledger_outbox`` is written in the SAME transaction as the action it records, so an event exists
exactly when its action committed. The shipper reads it across tenants through the worker's owner
connection and marks ``shipped_archive_at``; rows are deleted seven days after that.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

CONSENT_STATUSES = ("on", "off", "pending")
CONSENT_SOURCES = ("signup", "prompt", "settings")


class TrainingConsent(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "training_consents"
    __table_args__ = (Index("ix_training_consent_tenant_decided", "tenant_id", "decided_at"),)

    status: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(10))
    terms_version: Mapped[str] = mapped_column(String(20))
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(TZDateTime())


class LedgerOutbox(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "ledger_outbox"
    __table_args__ = (
        Index("uq_ledger_outbox_event", "event_id", unique=True),
        Index("ix_ledger_outbox_shipping", "shipped_archive_at", "occurred_at"),
    )

    event_id: Mapped[str] = mapped_column(String(26))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime())
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    shipped_archive_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
