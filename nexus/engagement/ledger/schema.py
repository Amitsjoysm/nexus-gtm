"""Each store's schema, as versioned SQL files applied from the Control plane (spec §18.2).

The files live in ``nexus/engagement/ledger/sql/<store>/`` and are applied in name order inside one
transaction each, recorded in ``nexus_ledger.schema_migrations``. Never by hand: a store at a version
the code does not know is how a shipper silently writes into a table that no longer means what it
did. Every file is idempotent (``CREATE ... IF NOT EXISTS``), so a half-applied version can be
re-applied rather than repaired.

This deliberately does NOT use Alembic. Alembic owns the app's own database and its chain is
replayed onto an empty database by a test; these are three foreign databases with one table group
each, applied by an operator pressing a button, and giving them a second Alembic environment with
its own head would be a chain nobody replays.
"""
from __future__ import annotations

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"


def migrations(store: str) -> list[tuple[str, str]]:
    """``[(version, sql)]`` for a store, in name order. Empty for an unknown store."""
    folder = SQL_DIR / store
    if not folder.is_dir():
        return []
    return [(path.stem, path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.sql"))]


async def applied_versions(conn) -> set[str]:
    """What the store says it has. An absent table means nothing is applied yet, not an error."""
    if await conn.fetchval("SELECT to_regclass('nexus_ledger.schema_migrations')") is None:
        return set()
    rows = await conn.fetch("SELECT version FROM nexus_ledger.schema_migrations")
    return {row["version"] for row in rows}


async def apply_schema(store: str) -> list[str]:
    """Apply every pending version. Returns the versions applied by THIS call, so pressing the
    button twice reports an empty list rather than claiming work it did not do."""
    from nexus.engagement.ledger.stores import connect

    applied: list[str] = []
    async with connect(store) as conn:
        done = await applied_versions(conn)
        for version, sql in migrations(store):
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO nexus_ledger.schema_migrations (version) VALUES ($1) "
                    "ON CONFLICT (version) DO NOTHING",
                    version,
                )
            applied.append(version)
    return applied
