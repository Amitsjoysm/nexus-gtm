"""C-1 on real Postgres: the scheduler advisory lock is mutually exclusive, and the discovery
claim serializes concurrent workers via row-level locking."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests_integration.conftest import PG_URL, requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]


@pytest_asyncio.fixture
async def pg_engine():
    engine = create_async_engine(PG_URL, pool_size=5, max_overflow=5)
    # `test_orm_for_update_compiles_on_postgres` needs a real `tenants` table to lock. In the
    # Docker stack the app container has already migrated this database, which is what the test
    # used to assume; CI starts a bare postgres service and nothing provisions it, so the test
    # died on UndefinedTableError the moment it began running at all. `checkfirst=True` keeps
    # both environments correct, and creating only the one table this suite touches stops it
    # becoming a second copy of the migration chain that can drift from the real one.
    from nexus.models.identity import Tenant

    async with engine.begin() as conn:
        await conn.run_sync(Tenant.__table__.create, checkfirst=True)
    yield engine
    await engine.dispose()


async def test_scheduler_advisory_lock_is_mutually_exclusive(pg_engine):
    """Two workers cannot both hold the scheduler lock — exactly one enqueues per tick."""
    from nexus.workers.scheduler import _acquire_scheduler_lock, _release_scheduler_lock

    async with AsyncSession(pg_engine) as s1, AsyncSession(pg_engine) as s2:
        # Force distinct connections so the advisory lock (connection-scoped) is a real contest.
        await s1.execute(text("SELECT 1"))
        await s2.execute(text("SELECT 1"))

        assert await _acquire_scheduler_lock(s1) is True   # first worker becomes leader
        assert await _acquire_scheduler_lock(s2) is False  # second is a follower this tick

        await _release_scheduler_lock(s1)                  # leader finishes its tick
        assert await _acquire_scheduler_lock(s2) is True   # follower can now lead
        await _release_scheduler_lock(s2)


async def test_orm_for_update_compiles_on_postgres(pg_engine):
    """The ORM `FOR UPDATE` the discovery claim relies on emits valid SQL and executes against
    real Postgres (on SQLite it is silently a no-op, so this path is only exercised here). The
    `tenants` table already exists — the app container ran migrations on this database."""
    from nexus.models.identity import Tenant

    async with AsyncSession(pg_engine) as s:
        # Mirrors the working advisory-lock test's execute pattern (no session.begin()/get(),
        # which trip a greenlet edge case under the local Python 3.14 runtime; CI uses 3.10).
        result = await s.execute(select(Tenant).with_for_update().limit(1))
        _ = result.first()  # executing without error is the assertion (FOR UPDATE valid on PG)
        await s.rollback()


async def test_row_lock_is_exclusive(pg_engine):
    """The FOR UPDATE row lock the claim relies on is genuinely exclusive: while one transaction
    holds it, a second `FOR UPDATE NOWAIT` on the same row fails immediately — so two workers
    cannot both claim the same per-interval discovery slot. Verified with raw asyncpg to keep the
    lock-contention assertion out of SQLAlchemy's async-session greenlet machinery."""
    import asyncpg

    dsn = PG_URL.replace("postgresql+asyncpg://", "postgresql://")
    c1 = await asyncpg.connect(dsn)
    c2 = await asyncpg.connect(dsn)
    try:
        await c1.execute("CREATE TABLE IF NOT EXISTS _c1_locktest (id int primary key)")
        await c1.execute("INSERT INTO _c1_locktest VALUES (1) ON CONFLICT DO NOTHING")

        tr1 = c1.transaction()
        await tr1.start()
        await c1.execute("SELECT id FROM _c1_locktest WHERE id = 1 FOR UPDATE")  # holds the lock

        tr2 = c2.transaction()
        await tr2.start()
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await c2.execute("SELECT id FROM _c1_locktest WHERE id = 1 FOR UPDATE NOWAIT")
        await tr2.rollback()

        await tr1.rollback()  # holder releases
        tr3 = c2.transaction()
        await tr3.start()
        row = await c2.fetchval("SELECT id FROM _c1_locktest WHERE id = 1 FOR UPDATE NOWAIT")
        assert row == 1  # lock now acquirable
        await tr3.rollback()
    finally:
        await c1.execute("DROP TABLE IF EXISTS _c1_locktest")
        await c1.close()
        await c2.close()
