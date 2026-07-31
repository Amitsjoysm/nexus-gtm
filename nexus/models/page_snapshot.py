# nexus/models/page_snapshot.py
"""What a watched page looked like last time we checked.

The state that turns a fetch into a *change* signal. Without a stored baseline every check reports
"here is a pricing page", which is not news; with one it reports "their pricing page changed", which
is a reason to call.

Tenant-scoped, so `scripts/apply_rls.py` enrols it automatically — two workspaces watching the same
company keep independent baselines, which is correct: they started watching on different days and a
shared baseline would hand one of them the other's change history.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime, utcnow
from nexus.core.tenancy import TenantScoped

# Page kinds worth watching. Mirrors WATCHED_PAGES in nexus/ingestion/webwatch.py.
PAGE_KINDS = ("pricing", "security", "careers", "about")


class PageSnapshot(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "page_snapshots"
    __table_args__ = (
        # One baseline per page kind per account per tenant. The unique constraint is what makes
        # the check idempotent: a re-run updates the baseline instead of accumulating rows and
        # comparing against an arbitrary one of them.
        UniqueConstraint("tenant_id", "account_id", "page_kind", name="uq_page_snapshot"),
        Index("ix_page_snapshot_changed", "tenant_id", "last_changed_at"),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    page_kind: Mapped[str] = mapped_column(String(20), index=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    # sha256 of the NORMALISED text, not the raw HTML. Raw HTML is unstable across fetches seconds
    # apart — see the webwatch module docstring.
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # The normalised text itself, so the next change can be described rather than merely detected.
    # A diff needs both sides, and re-fetching the old version is not possible.
    content: Mapped[str] = mapped_column(Text, default="")
    # Human summary of the most recent change, e.g. "Added: Enterprise custom pricing (+3 words)."
    last_change_summary: Mapped[str] = mapped_column(Text, default="")
    change_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    # Null until the page changes at least once — a first sighting is a baseline, not an event.
    last_changed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
