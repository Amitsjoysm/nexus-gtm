"""Which signal kinds a workspace wants collected.

A tester asked why signals they had never enabled were appearing, and — the sharper question —
whether they were being billed for them. `signal_sources` is a **deployment-global** setting naming
which *collectors* run; there was no per-tenant control over what gets kept at all.

**The absence of a row means the kind is enabled.** That is what makes this table additive: every
existing workspace has no rows and keeps every signal it gets today. Same rule as
`notification_preferences`, and the same reason — a preferences table that mutes people by existing
is a silent regression for every customer who never opened the screen.

Tenant-scoped, so `scripts/apply_rls.py` enrols it on deploy with no manual policy work.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped


class SignalPreference(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "signal_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", name="uq_signal_pref_kind"),)

    # Matches `nexus.ingestion.service.SIGNAL_KINDS`. Stable strings: renaming one silently
    # re-enables whatever a workspace had switched off, because the old row stops matching.
    kind: Mapped[str] = mapped_column(String(60), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
