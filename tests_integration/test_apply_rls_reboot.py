"""`scripts/apply_rls.py` on a database that already has RLS, against REAL Postgres.

Found on staging, 2026-11-06: every boot re-enabled RLS and dropped and re-created the policy on
every tenant table. Each of those statements takes an ACCESS EXCLUSIVE lock, so on a live database
it waits for every open transaction on that table; the new revision overran its liveness check on
every attempt and v1.13.0 never started. These prove a boot now:

* changes nothing on a database already set up (the policies keep their oids),
* finishes while another connection holds a read transaction on a tenant table, which the old
  script would have waited on for as long as that transaction stayed open,
* still repairs a table whose RLS was switched off, whose policy was dropped, or whose policy says
  something else.

Skipped unless ``NEXUS_TEST_POSTGRES_URL`` is set. A throwaway database and role, never
``nexus_app``, as in ``test_prospector_rls.py``.
"""
from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests_integration.conftest import PG_URL, requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]

ROLE = "nexus_app_reboot_test"
PASSWORD = "apply-rls-reboot-test-password"
DATABASE = "nexus_apply_rls_reboot_test"


def _admin_dsn(database: str = "") -> str:
    dsn = PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    return dsn.rsplit("/", 1)[0] + "/" + database if database else dsn


def _load_apply_rls():
    path = Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("_apply_rls_reboot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _drop() -> None:
    admin = await asyncpg.connect(_admin_dsn(), timeout=20)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    finally:
        await admin.close()


@pytest_asyncio.fixture
async def provisioned(monkeypatch):
    """A fresh database with the whole schema and RLS applied once; yields the loaded script."""
    from nexus.core import db as core_db
    from nexus.core.config import get_settings

    await _drop()
    admin = await asyncpg.connect(_admin_dsn(), timeout=20)
    await admin.execute(f'CREATE DATABASE "{DATABASE}"')
    await admin.close()

    owner_url = PG_URL.rsplit("/", 1)[0] + "/" + DATABASE
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", owner_url)
    monkeypatch.setattr(settings, "db_owner_url", "")
    await core_db.dispose_db()
    await core_db.init_db()
    await core_db.dispose_db()

    script = _load_apply_rls()
    monkeypatch.setattr(script, "APP_ROLE", ROLE)
    monkeypatch.setenv("NEXUS_APP_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("NEXUS_DB_OWNER_URL", owner_url)
    await script.main()

    yield script

    await core_db.dispose_db()
    await _drop()


async def _policies() -> dict[str, int]:
    owner = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    try:
        rows = await owner.fetch(
            "SELECT c.relname, p.oid FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
            "WHERE p.polname = 'tenant_isolation'")
    finally:
        await owner.close()
    return {r["relname"]: r["oid"] for r in rows}


async def test_a_second_boot_changes_nothing(provisioned, capsys):
    before = await _policies()
    assert "accounts" in before and len(before) > 50
    capsys.readouterr()

    await provisioned.main()

    assert await _policies() == before  # same oids: no policy was dropped and re-created
    assert "0 set up now" in capsys.readouterr().out


async def test_a_boot_does_not_wait_for_open_transactions(provisioned):
    reader = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    tx = reader.transaction()
    await tx.start()
    try:
        # An open transaction holding ACCESS SHARE on a tenant table, as the running app and
        # worker always do. ALTER TABLE ... ENABLE ROW LEVEL SECURITY would wait on it.
        await reader.fetchval("SELECT count(*) FROM accounts")
        await asyncio.wait_for(provisioned.main(), timeout=30)
    finally:
        await tx.rollback()
        await reader.close()


async def test_a_boot_repairs_only_the_tables_that_drifted(provisioned, capsys):
    owner = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    try:
        await owner.execute('ALTER TABLE "contacts" DISABLE ROW LEVEL SECURITY')
        await owner.execute('DROP POLICY tenant_isolation ON "signal_events"')
        await owner.execute('DROP POLICY tenant_isolation ON "prospect_lists"')
        await owner.execute('CREATE POLICY tenant_isolation ON "prospect_lists" USING (true)')
    finally:
        await owner.close()
    before = await _policies()
    capsys.readouterr()

    await provisioned.main()

    assert "3 set up now" in capsys.readouterr().out
    after = await _policies()
    assert {name for name in after if after[name] != before.get(name)} == {
        "contacts", "signal_events", "prospect_lists"}

    owner = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    try:
        query = provisioned._POLICY_STATE.text.replace("CAST(:names AS text[])", "$1::text[]")
        rows = await owner.fetch(query, ["contacts", "signal_events", "prospect_lists"])
    finally:
        await owner.close()
    assert len(rows) == 3 and all(
        provisioned.policy_in_place(SimpleNamespace(**dict(r))) for r in rows)
