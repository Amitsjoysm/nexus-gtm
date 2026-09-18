"""Cached web fetches and search results, shared by every tenant.

Signal collection asks the same questions about the same company every six hours, and two
workspaces tracking one company ask them twice. The answer is the same fact for all of them, so it
is bought once and reused until it expires.

**Platform-global on purpose: no `tenant_id`.** `scripts/apply_rls.py` enrols any table that has
one, and enrolling this would return zero rows to the shared reader — a silent miss, not an error.
Nothing tenant-specific may ever be written here: a row is a public web result, keyed by the query
or URL that produced it.

`id` is a sha256 of the normalised subject plus the engine and limit, so two workers racing on one
query produce the same key and one insert loses cleanly instead of splitting the entry.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, TimestampMixin, TZDateTime

#: What a row holds. `search` is a list of hits; `page` is one fetched document.
CACHE_KINDS = ("search", "page")


class WebCache(TimestampMixin, Base):
    __tablename__ = "web_cache"
    __table_args__ = (
        # The prune sweep's only scan.
        Index("ix_web_cache_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="search")
    #: The normalised query or URL, readable so an operator can answer "what did we buy?".
    subject: Mapped[str] = mapped_column(Text, default="")
    #: Which backend answered, e.g. `nexusfetch:ddg` or `firecrawl`.
    engine: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict | list] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    #: Reuses, so the saving is measurable rather than asserted.
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
