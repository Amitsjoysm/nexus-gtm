# nexus/models/jobs.py
"""Dead-lettered background jobs.

Where work goes to be *kept* rather than lost. When a job has exhausted its attempts, the whole
envelope is parked here — name, payload, last error, attempt count — so an operator can fix the
cause and replay it from the admin API.

**Platform-global on purpose.** There is no ``tenant_id`` column. ``scripts/apply_rls.py``
enrols every table that has one into Row-Level Security, and a dead letter must be readable by
two parties who have no tenant binding at all: the worker that writes it (running a cross-tenant
sweep) and the platform operator who triages it. An RLS policy here would not error — it would
silently return zero rows, which is the failure mode CLAUDE.md warns about. The affected tenant
is recorded as ``subject_tenant_id``, matching ``billing_audit_log`` and
``billing_webhook_events``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TZDateTime, utcnow


class DeadLetterJob(IdMixin, Base):
    __tablename__ = "dead_letter_jobs"
    __table_args__ = (
        # The triage query: open dead letters, optionally narrowed to one job name.
        Index("ix_dead_letter_jobs_open", "job_name", "replayed_at"),
    )

    job_name: Mapped[str] = mapped_column(String(80), index=True)
    # The exact payload the handler was given, so a replay is a faithful re-run and not a
    # reconstruction from memory.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # sha256 of (name, payload). A job that keeps failing the same way updates one row's
    # last_seen_at instead of piling up thousands of identical ones.
    payload_digest: Mapped[str] = mapped_column(String(64), default="", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Nullable, and NOT called tenant_id — see the module docstring.
    subject_tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    # Set when an operator replays it; a replayed row stops matching the dedupe lookup, so a
    # fresh failure of the same job opens a new dead letter rather than reviving the old one.
    replayed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
