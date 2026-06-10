"""Database engine, session factory, and declarative base.

Tenancy enforcement lives in ``nexus.core.tenancy``; this module is tenant-agnostic
infrastructure only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nexus.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime | None) -> datetime | None:
    """Normalize a datetime to tz-aware UTC.

    Columns persisted to SQLite (and any ``DateTime`` without ``timezone=True``) come back
    naive, while :func:`utcnow` is tz-aware. Mixing the two in arithmetic raises
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``. Treat a naive
    value as UTC so comparisons are always safe.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class TZDateTime(TypeDecorator):
    """A ``DateTime(timezone=True)`` that always returns tz-aware UTC datetimes.

    SQLite stores datetimes as strings and does not preserve timezone info on read,
    returning naive ``datetime`` objects. This decorator re-attaches UTC so that
    code using the ORM never has to call ``ensure_aware`` manually.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return ensure_aware(value)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if settings.is_postgres:
            kwargs.update(pool_size=10, max_overflow=20)
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def init_db() -> None:
    """Create tables. In prod, prefer Alembic migrations; this is for dev/test bootstrap."""
    import nexus.models  # noqa: F401  (register mappers)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
