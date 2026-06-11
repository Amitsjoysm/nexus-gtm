"""Signal events — normalized 1st- and 3rd-party GTM signals."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, utcnow
from nexus.core.tenancy import TenantScoped

# Built-in signal taxonomy.
SIGNAL_KINDS = {
    "funding",        # raised a round
    "news",           # press / announcement
    "job_posting",    # hiring for relevant roles
    "hiring",         # headcount growth
    "g2_intent",      # 3rd-party intent (G2)
    "web_visit",      # 1st-party website visit
    "job_switch",     # champion changed companies
    "product_usage",  # 1st-party product signal
    "call",           # call/meeting signal
    "tech_install",   # added a relevant technology
}


class SignalEvent(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "signal_events"
    # Idempotent ingestion: the same external signal can be ingested once per tenant.
    # The (tenant, occurred_at) index backs the activity feed's "newest N for this
    # tenant" read so it never sorts the tenant's full signal history per poll.
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_signal_dedupe"),
        Index("ix_signal_tenant_occurred", "tenant_id", "occurred_at"),
    )

    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True, nullable=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(400))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    strength: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1 intrinsic importance
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
