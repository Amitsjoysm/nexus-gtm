"""LinkedIn prospecting state: how far through each search we have read, and each populate request.

* ``ProspectCursor`` is **platform-global** (no ``tenant_id``). Every page the company-search actor
  returns is stored in the shared ``companies`` table, so a second workspace with the same ICP
  receives those companies from the database step. A per-workspace cursor would make it pay to read
  the same page again for companies it has already been offered for free.
* ``ProspectRun`` is **tenant-scoped**: one workspace's request for N companies, its progress, and
  why anything found was not delivered. ``scripts/apply_rls.py`` enrols it by its ``tenant_id``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

RUN_KINDS = ("populate", "daily", "similar")
RUN_STATUSES = ("queued", "running", "done", "failed")


class ProspectCursor(TimestampMixin, Base):
    __tablename__ = "prospect_cursors"

    #: sha1 of the canonical actor query (industries, countries, size band, keywords), so two
    #: workers asking the same question read and advance one row.
    query_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: The query itself, readable, so "what did we search?" has an answer.
    query: Mapped[dict] = mapped_column(JSON, default=dict)
    #: The next page to read, 1-based like the actor's ``startPage``.
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    #: From the actor's pagination: at most 20 pages of 50, the 1,000-result ceiling.
    total_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: LinkedIn's full count, which can exceed what one query may page through.
    total_results: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_read_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class ProspectRun(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospect_runs"
    __table_args__ = (Index("ix_prospect_runs_tenant_created", "tenant_id", "created_at"),)

    kind: Mapped[str] = mapped_column(String(16), default="populate")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    #: How many came from each source: {"database": n, "linkedin": n, "web": n}.
    sources: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Everything found and not delivered, by reason: {"no_website": n, "already_held": n, ...}.
    discarded: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Why a source contributed nothing: {"linkedin": "not_configured" | "failed" | ...}.
    notes: Mapped[dict] = mapped_column(JSON, default=dict)
    account_ids: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
