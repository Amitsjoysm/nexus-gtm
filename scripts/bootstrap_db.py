"""Create-or-migrate database bootstrap for production deploys.

The bootstrap is state-dependent:

  * fresh database (no tables)          -> create_all + ``alembic stamp head``
  * tables but no alembic_version       -> ``alembic stamp head`` (create_all-origin DB)
  * stamped database                    -> ``alembic upgrade head`` (normal upgrade path)

Historical note: the "create" branch used to be *required*, because the old ``0001_initial``
called ``Base.metadata.create_all()`` and so pre-created tables that later revisions then failed
to create — the chain could not be replayed onto an empty database at all. That is fixed
(``0020_baseline_schema`` is frozen literal DDL, and ``tests/test_migrations_replay.py`` proves
the chain rebuilds the schema exactly). The branch is kept because it is faster than replaying
25 revisions and because the "stamp" branch is still needed for databases created by create_all
before migrations existed.

Switching the fresh-database path to a plain ``alembic upgrade head`` would make deploys
exercise the chain itself, which is the stronger guarantee — a deliberate change to make on its
own, not as a side effect.

Run inside the app image: ``python scripts/bootstrap_db.py``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import subprocess
import sys

from sqlalchemy import inspect


def decide(table_names: set[str]) -> str:
    """Pick the bootstrap action from the database's current table set."""
    if not table_names:
        return "create"
    if "alembic_version" not in table_names:
        return "stamp"
    return "upgrade"


async def _table_names() -> set[str]:
    from nexus.core.db import get_engine

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True)


# A stable arbitrary 63-bit key for the schema lock. Distinct from the scheduler's
# (nexus/workers/scheduler.py) so the two never contend.
_SCHEMA_LOCK_KEY = 0x4E455853_5343484D & 0x7FFFFFFFFFFFFFFF


@asynccontextmanager
async def _schema_lock():
    """Serialize schema work across app replicas.

    Since M27 the app runs TWO replicas and both start with NEXUS_RUN_MIGRATIONS=1. Without this,
    two concurrent `alembic upgrade head` runs race on one `alembic_version` row — which is how a
    half-applied schema happens, on the deploy where you least want one.

    A **session** advisory lock, not a transaction one: it spans the alembic subprocess, and
    Postgres releases it automatically if the process dies, so a replica that crashes mid-migration
    cannot wedge every future deploy. Non-Postgres backends are single-process by construction and
    take no lock.
    """
    from sqlalchemy import text

    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        if session.get_bind().dialect.name != "postgresql":
            yield
            return
        print("[bootstrap] waiting for the schema lock...")
        # Blocking, not `try_`: the other replica IS migrating, and the right behaviour is to wait
        # for it and then observe an already-current schema — not to skip and start serving.
        await session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _SCHEMA_LOCK_KEY})
        try:
            yield
        finally:
            try:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _SCHEMA_LOCK_KEY}
                )
            except Exception:  # a failed unlock self-heals when the connection closes
                pass


async def main() -> None:
    import nexus.models  # noqa: F401  (register all mappers)

    async with _schema_lock():
        await _run()


async def _run() -> None:
    from nexus.core.db import init_db

    action = decide(await _table_names())
    if action == "create":
        print("[bootstrap] fresh database: creating schema from models + stamping head")
        await init_db()  # Base.metadata.create_all
        _alembic("stamp", "head")
    elif action == "stamp":
        print("[bootstrap] unstamped create_all database: stamping head")
        _alembic("stamp", "head")
    else:
        print("[bootstrap] stamped database: alembic upgrade head")
        _alembic("upgrade", "head")
    print("[bootstrap] done")


if __name__ == "__main__":
    asyncio.run(main())
