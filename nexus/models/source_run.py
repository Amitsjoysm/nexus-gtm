# nexus/models/source_run.py
"""Crawl history: one row per source, per account, per refresh.

The gap this closes: signal collection produced signals or it produced nothing, and nothing was
the same observation either way. "Did the funding source run for this account?" had no answer —
not in the database, not in the API, only in whatever log lines had not yet rotated away. So a
source that had been silently failing for a week was indistinguishable from a quiet market, and
the first sign of trouble was a rep asking why an obviously-funded account showed no round.

A run row is written whether or not signals came out of it, which is the entire point: the
**absence** of signals becomes evidence rather than silence.

Tenant-scoped, so `apply_rls.py` enrols it automatically — unlike `dead_letter_jobs`, a crawl
belongs to exactly one workspace and its operators are that workspace's users.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime, utcnow
from nexus.core.tenancy import TenantScoped

# Terminal states of one source run.
SOURCE_RUN_OUTCOMES = ("ok", "empty", "timeout", "error")


class SignalSourceRun(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "signal_source_runs"
    __table_args__ = (
        # The two questions actually asked of this table: "what happened for this account?" and
        # "is this source healthy right now?".
        Index("ix_source_run_account", "tenant_id", "account_id", "started_at"),
        Index("ix_source_run_health", "tenant_id", "source", "started_at"),
    )

    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True, nullable=True
    )
    source: Mapped[str] = mapped_column(String(60), index=True)
    # ok | empty | timeout | error. `empty` is deliberately not `ok`: a source that runs cleanly
    # and finds nothing every single time is a broken source, and merging the two states hides
    # exactly that.
    outcome: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    # Raw items the source returned, before dedupe against what is already stored.
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    # ...and how many survived to become new signals. A large gap between the two means the source
    # is re-finding the same event, which is the cost signature worth watching.
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    # Provenance: what the source was actually asked. For a search-backed source these are the
    # rendered queries — without them, "why did this find nothing?" is unanswerable after the fact,
    # because the query depends on the account, the date and the provider's dialect.
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
