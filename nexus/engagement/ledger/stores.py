"""Connections to the three ledger stores (spec §18.2, §18.3, D27).

Each store is its own Postgres — three Supabase projects — reached with a role that owns the
``nexus_ledger`` schema and nothing else (``docs/engagement/setup-supabase.md``). A connection is
opened per batch and closed after: shipping runs on the heartbeat, and a pool held between ticks
would hold connections Supabase counts against the project all day for a job that runs for a second.

Three details that are not style choices:

* **``statement_cache_size=0``.** Supabase's pooler can run in transaction mode, where the server
  connection a prepared statement was created on is not the one the next query lands on. asyncpg's
  cache then raises ``InvalidSQLStatementNameError`` under load and never in a quiet test.
* **The DSN goes through ``validate_dsn``**, the SSRF guard the source-database subsystem already
  owns. A connection string stored in the Control plane is operator input, and "connect to this and
  tell me what happened" is a port scanner otherwise.
* **The password never reaches a log or an audit row** — ``redact_dsn`` is what any reporting uses.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

STORES = ("archive", "training", "insights")


class StoreNotConfigured(RuntimeError):
    """No connection string is stored for this ledger store."""


class StoreUnavailable(RuntimeError):
    """The store is configured but refused or could not be reached."""


def _allow_private() -> bool:
    from nexus.core.config import get_settings

    settings = get_settings()
    return settings.env in ("local", "test") or bool(settings.source_db_allow_private)


@asynccontextmanager
async def connect(store: str):
    """One asyncpg connection to ``store``. Raises ``StoreNotConfigured`` when there is no DSN."""
    from nexus.engagement import config
    from nexus.sources.safety import validate_dsn

    if store not in STORES:
        raise ValueError(f"unknown ledger store {store!r}")
    dsn = await config.ledger_dsn(store)
    if not dsn:
        raise StoreNotConfigured(store)
    validate_dsn(dsn, allow_private=_allow_private())
    # Imported only once there is a DSN to use. asyncpg is the `postgres` extra, so a deployment
    # without it raised ImportError here BEFORE the "no DSN" check, and `status` reported an
    # unconfigured store as configured (found by CI, which installs without that extra).
    import asyncpg
    conn = await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=20, command_timeout=60, statement_cache_size=0,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def status(store: str) -> dict:
    """What an operator needs to see on the Ledger tab: configured, reachable, owns its schema,
    which versions are applied and which are pending. Never raises, and never leaks the DSN."""
    from nexus.engagement.ledger import schema

    report = {
        "store": store, "configured": False, "reachable": False, "owns_schema": False,
        "applied": [], "pending": [version for version, _sql in schema.migrations(store)],
        "detail": "",
    }
    try:
        async with connect(store) as conn:
            report["configured"] = True
            report["reachable"] = True
            owner = await conn.fetchval(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'nexus_ledger'")
            role = await conn.fetchval("SELECT current_user")
            report["owns_schema"] = bool(owner) and owner == role
            done = await schema.applied_versions(conn) if owner else set()
            report["applied"] = sorted(done)
            report["pending"] = [v for v, _sql in schema.migrations(store) if v not in done]
            if owner and owner != role:
                report["detail"] = (
                    f"schema nexus_ledger is owned by {owner}, not {role}; see "
                    "docs/engagement/setup-supabase.md")
            elif not owner:
                report["detail"] = "schema nexus_ledger does not exist yet — apply the schema"
    except StoreNotConfigured:
        report["detail"] = "not configured"
    except Exception as exc:
        # Configured, since we had a DSN to try: the useful distinction is reachable or not.
        report["configured"] = True
        report["detail"] = f"{type(exc).__name__}: could not connect or read the schema"
    return report
