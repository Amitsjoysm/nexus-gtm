"""Database engine, session factory, and declarative base.

Tenancy enforcement lives in ``nexus.core.tenancy``; this module is tenant-agnostic
infrastructure only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, event, func
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
            kwargs.update(
                pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow
            )
        elif settings.is_sqlite:
            # Wait up to 30s for a lock instead of erroring immediately: the API and the
            # always-on worker can write the same file concurrently in dev. Pairs with the
            # WAL pragma below (concurrent readers + one writer). No effect on Postgres/prod.
            kwargs["connect_args"] = {"timeout": 30}
        _engine = create_async_engine(settings.database_url, **kwargs)
        if settings.is_sqlite:
            _install_sqlite_pragmas(_engine)
    return _engine


_platform_engine: AsyncEngine | None = None
_platform_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_platform_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """A session for genuinely cross-tenant PLATFORM work, bypassing RLS.

    The API connects as the least-privilege ``nexus_app`` role, whose RLS policies read
    ``app.current_tenant``. That is correct for everything tenant-facing, but it silently
    returns ZERO ROWS for the few operations that legitimately span tenants — listing every
    workspace's subscription in the staff console, or matching an incoming payment webhook to
    the invoice it paid, which arrives with no tenant context at all. Those queries do not
    error; they come back empty, which is the dangerous failure mode.

    ``NEXUS_DB_OWNER_URL`` is already provisioned to the app container for exactly this. Falls
    back to the normal URL when unset (SQLite in dev and tests, where there is no RLS anyway).

    Use this ONLY behind ``require_platform_admin`` or webhook signature verification. Anything
    tenant-facing must keep using ``get_sessionmaker`` + ``TenantSession``.
    """
    global _platform_engine, _platform_sessionmaker
    if _platform_sessionmaker is None:
        settings = get_settings()
        url = settings.db_owner_url or settings.database_url
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("postgresql"):
            # Small pool: platform reads are rare compared with tenant traffic. Still counts
            # against the server's max_connections — see Settings.db_platform_pool_size.
            kwargs.update(
                pool_size=settings.db_platform_pool_size,
                max_overflow=settings.db_platform_max_overflow,
            )
        _platform_engine = create_async_engine(url, **kwargs)
        _platform_sessionmaker = async_sessionmaker(
            _platform_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _platform_sessionmaker


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Enable WAL journaling + a busy timeout on every new SQLite connection.

    WAL lets readers proceed during a write (default rollback-journal blocks them); busy_timeout
    makes a contending writer wait rather than raise ``database is locked``. Both are concurrency
    robustness only — query results are unchanged. No-op on ``:memory:`` (WAL needs a file).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


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
