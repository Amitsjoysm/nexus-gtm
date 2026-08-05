# nexus/models/source_db.py
"""Registered external source databases — platform-global, superadmin-owned.

**No ``tenant_id``, on purpose**, exactly like ``companies`` / ``people``. ``scripts/apply_rls.py``
enrols any table that has one, and enrolling this would hide every source from the platform code
that must read it — silently, as zero rows rather than an error, which is the trap CLAUDE.md opens
with. Everything here runs through ``get_platform_sessionmaker()``.

The decision this schema encodes (plan, 2026-08-04): **platform-wide only, never per-tenant.**
Results land in the shared `companies` / `people` stores, so a mis-mapped source is wrong for every
tenant at once. That is precisely why `status` exists and why it does not start at `active`.

``status`` is the staging gate, and it is the same shape as the shared company crawl's
shadow → diff → fan-out ladder, for the same reason: this subsystem has shipped six
wrong-attribution bugs, and four were found only by running against live data.

    registered -> connected -> introspected -> mapped -> verified   (then, and only then, usable)

A source is consumable **only** at ``verified``, which is reachable only by a dry run that actually
read rows through the mapping. Nothing in this model lets an admin skip a rung: `status` is
advanced by the service functions that do the work, never set from a request body.

``dsn_encrypted`` is Fernet-sealed (``nexus/sources/crypto.py``) and never leaves the server.
``last_ok_at`` / ``last_error`` are here so a source that has quietly stopped working is visible —
without them, "the vendor rotated our password three weeks ago" reads as "this data just isn't in
the source", which is the same class of failure as a broken signal source looking like a quiet
market.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# The staging ladder. Order matters: `SOURCE_STATUSES.index(...)` is how progress is compared.
SOURCE_STATUSES = ("registered", "connected", "introspected", "mapped", "verified", "failed")

# The only status at which a source may be read from. One name, one place, so "can we use this?"
# can never drift between the admin UI, the provider and the tests.
USABLE_STATUS = "verified"

# What a source can be. Only Postgres today; the column exists so adding MySQL later is a value,
# not a schema change. `safety.validate_dsn` independently refuses any other scheme.
SOURCE_KINDS = ("postgres",)


class SourceDatabase(IdMixin, TimestampMixin, Base):
    __tablename__ = "source_databases"
    __table_args__ = (
        # The provider's only scan: "which sources may I read from?"
        Index("ix_source_db_usable", "status", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="postgres", nullable=False)

    # Fernet ciphertext. Never selected into a response model; `redact_dsn` is what the console
    # shows. Text rather than String(n) because Fernet output grows with the DSN.
    dsn_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Host/port/database with credentials stripped, computed once at registration. Stored so the
    # console can identify a source without ever unsealing the DSN to render a list.
    dsn_redacted: Mapped[str] = mapped_column(String(400), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="registered", nullable=False)
    # Separate from `status` on purpose. `enabled` is an operator's kill switch — flip it off
    # during an incident and back on afterwards without re-running the whole ladder. Collapsing
    # them would mean disabling a source discards the proof that it works.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Introspection output: [{"schema": ..., "table": ..., "columns": [{"name","type"}...]}, ...].
    # Every identifier in here passed `require_identifier` before it was stored, and passes it
    # again before it reaches a query — a name is attacker-controlled if the attacker owns the
    # source database, and validating only on the way in trusts the database row.
    discovered_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # {"entity": "company", "schema": ..., "table": ..., "columns": {app_field: source_column}}
    mapping: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # What the last dry run produced: counts, a redacted sample, and what it would have written.
    dry_run: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_ok_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    def is_usable(self) -> bool:
        """Whether the enrichment provider may read from this source.

        Both conditions, and no third: verified by a dry run, and not switched off. Callers must
        use this rather than testing `status` alone — an operator who disabled a source during an
        incident has said "do not read this", and a provider that checked only `status` would
        keep reading it.
        """
        return self.status == USABLE_STATUS and bool(self.enabled)
