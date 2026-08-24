# nexus/models/provider_setting.py
"""Per-provider settings. Today that means the LLM model; the shape allows more later.

Provider-level rather than per-key: every key for a provider addresses the same model catalogue, so
storing it on ``provider_keys`` would let two keys disagree about a fact that has one answer.

No ``tenant_id`` — deployment-wide, like ``provider_keys``, so ``apply_rls.py`` leaves it alone and
reads go through ``get_platform_sessionmaker()``.
"""
from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin


class ProviderSetting(IdMixin, TimestampMixin, Base):
    __tablename__ = "provider_settings"

    provider: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # Empty means "no override" — the environment value applies, exactly as before this table.
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
