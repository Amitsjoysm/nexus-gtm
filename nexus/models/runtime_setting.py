# nexus/models/runtime_setting.py
"""A setting an operator changed at runtime, overriding the environment.

Platform-global: no ``tenant_id``, no RLS, read through ``get_platform_sessionmaker()``. Same shape
as ``provider_settings``, and for the same reason — this is deployment configuration, not customer
data.

The value is stored as text and coerced on read against the catalog's declared type. A JSON column
would let a boolean come back as the string ``"false"``, which is truthy, and would switch a setting
ON while the panel showed it off.
"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin


class RuntimeSetting(IdMixin, TimestampMixin, Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Why it was changed. Lands in the audit log too, but keeping it on the row means the panel can
    # show the reason beside the current value rather than only in a separate history.
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
