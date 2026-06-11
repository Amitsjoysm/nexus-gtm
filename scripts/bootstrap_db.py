"""Create-or-migrate database bootstrap for production deploys.

Migration 0001 materializes the CURRENT model metadata (create_all), so running the full
alembic chain on a fresh database would re-create tables that 0001 already built in their
latest shape. The correct bootstrap is therefore state-dependent:

  * fresh database (no tables)          -> create_all + ``alembic stamp head``
  * tables but no alembic_version       -> ``alembic stamp head`` (create_all-origin DB)
  * stamped database                    -> ``alembic upgrade head`` (normal upgrade path)

Run inside the app image: ``python scripts/bootstrap_db.py``.
"""
from __future__ import annotations

import asyncio
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


async def main() -> None:
    import nexus.models  # noqa: F401  (register all mappers)
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
