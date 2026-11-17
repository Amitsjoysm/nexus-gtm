"""Database-level tenant isolation: a least-privilege app role + Row-Level Security.

Defense-in-depth on top of the application-layer ``TenantSession`` filtering. Run by the DB
*owner* (set ``NEXUS_DATABASE_URL`` to the owner connection) AFTER migrations:

    python scripts/apply_rls.py

It is idempotent — safe to run on every deploy — and a no-op when ``NEXUS_APP_DB_PASSWORD``
is unset (so a single-role deploy keeps working).

Design
------
* The **API app** connects as ``nexus_app`` (``NOSUPERUSER NOBYPASSRLS``). Every tenant-scoped
  table gets RLS with a policy keyed on ``current_setting('app.current_tenant')`` — which the
  request/worker session layer sets via ``apply_rls()``. So even a query that forgot its
  ``WHERE tenant_id = ...`` cannot return another tenant's rows.
* ``memberships`` and ``workspaces`` are intentionally excluded: auth flows (login, switch,
  signup) read/write them *without* a tenant context by design (a user's memberships span
  tenants), so RLS there would lock users out. They remain protected by app-layer filtering.
* The **worker** connects as the owner (it runs trusted cross-tenant sweeps to find due work),
  so it is not constrained by RLS; its per-tenant processing is still ``TenantSession``-scoped.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

APP_ROLE = "nexus_app"

# The one expression every tenant policy holds, for USING and WITH CHECK alike.
POLICY_EXPR = "tenant_id = current_setting('app.current_tenant', true)"

# What is already in place, read from the catalog in ONE query. Every boot used to re-enable RLS
# and drop and re-create the policy on all ~100 tenant tables, and each of those statements takes
# an ACCESS EXCLUSIVE lock on its table: against a live database it waits for every open
# transaction the running app and worker hold on that table, and every query behind it waits too.
# On staging, 2026-11-06, that took the new revision past its liveness check (about 20 s) on every
# attempt, so v1.13.0 never started while the deploy reported success. Reading the catalog takes
# no table lock at all, so a boot now changes only the tables that need it: new ones, normally.
_POLICY_STATE = text(
    "SELECT c.relname AS name, c.relrowsecurity AS rls, p.polname IS NOT NULL AS has_policy, "
    "       p.polcmd::text AS cmd, p.polpermissive AS permissive, "
    "       p.polroles = '{0}'::oid[] AS to_public, "
    "       pg_get_expr(p.polqual, p.polrelid) AS qual, "
    "       pg_get_expr(p.polwithcheck, p.polrelid) AS with_check "
    "  FROM pg_class c "
    "  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
    "  LEFT JOIN pg_policy p ON p.polrelid = c.oid AND p.polname = 'tenant_isolation' "
    " WHERE c.relkind IN ('r', 'p') AND c.relname::text = ANY(CAST(:names AS text[]))"
)


def _meaning(expr: str | None) -> str:
    """A policy expression as it reads, not as Postgres stores it. The catalog holds its own
    normal form, `((tenant_id)::text = current_setting('app.current_tenant'::text, true))`, so the
    casts, brackets and spaces are dropped before comparing."""
    if not expr:
        return ""
    without_casts = re.sub(r"::[a-z ]+(\(\d+\))?", "", expr.lower())
    return re.sub(r"[()\s]", "", without_casts)


def policy_in_place(row) -> bool:
    """True when the table already enforces exactly the policy this script would create: RLS on,
    and `tenant_isolation` permissive, for every command, to PUBLIC, with the tenant expression on
    both sides. Anything else is set up again, which is what every boot used to do."""
    expected = _meaning(POLICY_EXPR)
    return bool(
        row.rls and row.has_policy and row.cmd == "*" and row.permissive and row.to_public
        and _meaning(row.qual) == expected and _meaning(row.with_check) == expected
    )


def _set_up_sql(table: str) -> str:
    """Enable RLS and (re)create the policy as ONE statement, so the app role is never left
    between the DROP and the CREATE with RLS on and no policy, which reads as zero rows."""
    return (
        "DO $$ BEGIN "
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY; '
        f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"; '
        f'CREATE POLICY tenant_isolation ON "{table}" '
        f"USING ({POLICY_EXPR}) WITH CHECK ({POLICY_EXPR}); "
        "END $$;"
    )

# Provisioning lock, held while this script rewrites roles, grants and policies. A stable
# arbitrary 63-bit key, distinct from bootstrap_db's schema lock and the scheduler's, so the
# three never contend with each other.
_PROVISION_LOCK_KEY = 0x4E455853_524C5301 & 0x7FFFFFFFFFFFFFFF
# Auth/identity tables read or written without a tenant context — never put RLS on these.
RLS_EXCLUDE = {"memberships", "workspaces"}


def _tenant_tables() -> list[str]:
    """Every tenant-scoped table (has a ``tenant_id`` column), minus the auth exclusions."""
    import nexus.models  # noqa: F401  (populate Base.metadata with all mappers)
    from nexus.core.db import Base

    return sorted(
        t.name
        for t in Base.metadata.sorted_tables
        if "tenant_id" in t.columns and t.name not in RLS_EXCLUDE
    )


def _owner_url() -> str:
    """The owner connection string. The entrypoint points NEXUS_DATABASE_URL at the owner for
    this script; fall back to the app setting for local/manual runs."""
    url = os.environ.get("NEXUS_DB_OWNER_URL") or os.environ.get("NEXUS_DATABASE_URL")
    if not url:
        from nexus.core.config import get_settings

        url = get_settings().database_url
    return url


async def main() -> None:
    app_password = os.environ.get("NEXUS_APP_DB_PASSWORD", "").strip()
    if not app_password:
        print("[apply_rls] NEXUS_APP_DB_PASSWORD not set — skipping (single-role deploy).")
        return

    url = _owner_url()
    if "+asyncpg" not in url and url.startswith("postgresql"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql"):
        print(f"[apply_rls] not a Postgres URL ({url.split('://')[0]}://…) — skipping.")
        return

    tables = _tenant_tables()
    pw = app_password.replace("'", "''")  # escape for the SQL literal (generated pw is hex, but be safe)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # Serialize across replicas, for the same reason bootstrap_db.py does. The app runs
            # TWO replicas (M27) and both entrypoints run this script; `GRANT ... ON ALL TABLES`
            # and `ALTER DEFAULT PRIVILEGES` rewrite shared catalog rows, and two of them at once
            # is `tuple concurrently updated` — which crashes the container and, because it is
            # raised from the ENTRYPOINT, takes both replicas down rather than one.
            #
            # `deploy/rollout.sh` staggers restarts and so never hit this, but a plain
            # `docker compose up` starts replicas together and does — the deploy path most likely
            # to be used in a hurry.
            #
            # Blocking rather than `try_`, and a distinct key from bootstrap's: the other replica
            # IS provisioning, and the right behaviour is to wait and then re-apply an idempotent
            # script, not to skip and start serving with privileges half-granted. Postgres drops
            # a session lock automatically if the process dies, so a crash cannot wedge a deploy.
            print("[apply_rls] waiting for the provisioning lock...")
            await conn.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _PROVISION_LOCK_KEY}
            )
            dbname = (await conn.execute(text("SELECT current_database()"))).scalar()

            # 1. Least-privilege app role (create once, then always reconcile password + flags).
            await conn.execute(
                text(
                    f"DO $$ BEGIN "
                    f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
                    f"    CREATE ROLE {APP_ROLE} LOGIN; "
                    f"  END IF; "
                    f"END $$;"
                )
            )
            # MANAGED POSTGRES IS NOT SUPERUSER. On Azure Database for PostgreSQL Flexible Server
            # (and RDS) the admin role is a member of azure_pg_admin / rds_superuser, NOT a real
            # superuser — and Postgres refuses NOSUPERUSER and NOBYPASSRLS from a non-superuser
            # even when setting them to the value the role ALREADY has:
            #   InsufficientPrivilegeError: permission denied to alter role
            #   DETAIL: Only roles with the SUPERUSER attribute may change the SUPERUSER attribute.
            #
            # Both are already the CREATE ROLE defaults, so dropping them changes nothing about the
            # role — but "it's the default" is not something to take on trust for the two attributes
            # that decide whether RLS is a tenant boundary or a decoration. BYPASSRLS in particular
            # would make every policy below silently inert. So: try the explicit form first (it is
            # the stronger statement of intent, and works on self-hosted Postgres), fall back to the
            # attributes a managed admin may actually set, and then VERIFY against pg_roles rather
            # than assume.
            base_attrs = "NOCREATEDB NOCREATEROLE NOINHERIT"
            try:
                await conn.execute(
                    text(
                        f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{pw}' "
                        f"NOSUPERUSER NOBYPASSRLS {base_attrs}"
                    )
                )
            except ProgrammingError as exc:
                if "permission denied to alter role" not in str(exc):
                    raise
                print(
                    "[apply_rls] non-superuser admin (managed Postgres): setting role attributes "
                    "without NOSUPERUSER/NOBYPASSRLS, then verifying."
                )
                await conn.execute(
                    text(f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{pw}' {base_attrs}")
                )

            # Verify, do not assume. A role that is superuser or has BYPASSRLS ignores every policy
            # this script creates, and nothing downstream would report it — cross-tenant reads would
            # simply succeed. Fail the deploy instead.
            row = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"
                    ),
                    {"r": APP_ROLE},
                )
            ).first()
            if row is None:
                raise RuntimeError(f"[apply_rls] role {APP_ROLE} missing after ALTER ROLE")
            if row.rolsuper or row.rolbypassrls:
                raise RuntimeError(
                    f"[apply_rls] REFUSING TO CONTINUE: {APP_ROLE} has "
                    f"rolsuper={row.rolsuper} rolbypassrls={row.rolbypassrls}. Row-level security "
                    f"would not be enforced for this role, so tenant isolation would be absent "
                    f"while appearing configured."
                )

            # 2. Privileges: connect + DML on existing and future objects (DDL stays with owner).
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{dbname}" TO {APP_ROLE}'))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f"TO {APP_ROLE}"
                )
            )
            await conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
                )
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
                )
            )

            # 3. RLS per tenant-scoped table. Non-FORCE: the owner (migrations, worker sweeps)
            #    bypasses; the app role is constrained to current_setting('app.current_tenant').
            #    Only tables not already enforcing the policy are touched (see _POLICY_STATE). A
            #    table missing from the catalog is set up too, which fails loudly, as it always did.
            state = {
                row.name: row
                for row in (await conn.execute(_POLICY_STATE, {"names": tables})).all()
            }
            to_set_up = [t for t in tables if t not in state or not policy_in_place(state[t])]
            for table in to_set_up:
                await conn.execute(text(_set_up_sql(table)))

            print(
                f"[apply_rls] role '{APP_ROLE}' reconciled; RLS enforced on {len(tables)} tables "
                f"({len(to_set_up)} set up now, {len(tables) - len(to_set_up)} already in place; "
                f"excluded: {', '.join(sorted(RLS_EXCLUDE))})."
            )
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _PROVISION_LOCK_KEY}
                )
            except Exception:  # a failed unlock self-heals when the connection closes
                pass
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # surface a clear failure so the deploy stops, not a silent half-apply
        print(f"[apply_rls] FAILED: {exc!r}", file=sys.stderr)
        raise
