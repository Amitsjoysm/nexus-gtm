# nexus/models/provider_key.py
"""Platform-wide provider API keys, managed from the Control plane.

**No ``tenant_id``.** These are deployment-wide credentials, not tenant data, so
``scripts/apply_rls.py`` leaves the table alone and everything reads it through
``get_platform_sessionmaker()``. Same rule as ``companies``, ``people`` and ``source_databases``,
and the reason is the one that keeps biting: an RLS miss returns zero rows rather than an error, so
enrolling this table would make every worker read come back empty and say nothing.

``status`` is written ONLY by ``nexus/providers/service.py``'s ``mark_tested`` / ``mark_failed``.
A request body never carries one — an admin who could write ``verified`` by hand could mark a dead
key working, which is the rule ``nexus/sources/service.py`` already enforces for the
source-database ladder.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# untested — never tested.
# probe_ok — the credential authenticates.
# verified — a real call through our own adapter succeeded.
# failed   — the last test, or a runtime call, rejected it.
#
# `probe_ok` and `verified` are separate on purpose, and this is the whole reason the feature has
# two test depths. Measured 2026-08-21: all five Groq keys returned 200 from GET /models and 404
# from every chat completion, because the configured model had been withdrawn. A single green
# state would have shown five healthy keys while every draft came from the stub and reached real
# prospects. Ordered weakest-to-strongest so progress can be compared.
KEY_STATUSES = ("untested", "probe_ok", "verified", "failed")


class ProviderKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "provider_keys"
    __table_args__ = (
        # The same key registered twice under one provider is always a mistake, and silently
        # accepting it would double that key's share of the rotation.
        UniqueConstraint("provider", "key_digest", name="uq_provider_key_digest"),
        Index("ix_provider_key_lookup", "provider", "enabled"),
    )

    provider: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_hint: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="untested", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_depth: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    # The provider's own error text, kept verbatim. "revoked" and "model withdrawn" arrive as
    # different messages behind similar statuses and need opposite fixes.
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    last_error_status: Mapped[int | None] = mapped_column(nullable=True)

    # Operator kill switch, separate from `status` — the same split as `source_databases.enabled`.
    # Disabling is never refused: during an incident "stop using this" must not be blocked by a
    # state machine.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The pinned key, tried first. At most one per provider, enforced in the service rather than by
    # a partial unique index — those are not portable to SQLite, where the offline suite runs.
    preferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
