"""A throwaway Postgres database with the whole schema, RLS applied, and the app on the app role.

The same setup the API runs under in staging and production: tenant requests as a least-privilege
role that Postgres RLS holds to one workspace, platform work as the owner. Each test file passes its
OWN database and role names, because the integration job runs files in parallel (``-n auto``) and
two files sharing a name drop each other's database mid-test (seen 2026-11-18). ``apply_rls``
resets the role's password, so it is never ``nexus_app``: a test must not do that to a server a
real deploy might share. Both are dropped afterwards.
"""
from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from tests_integration.conftest import PG_URL


def admin_dsn(database: str = "") -> str:
    dsn = PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    return dsn.rsplit("/", 1)[0] + "/" + database if database else dsn


def role_dsn(database: str, role: str, password: str) -> str:
    parsed = urlparse(admin_dsn())
    return f"postgresql://{role}:{password}@{parsed.hostname}:{parsed.port or 5432}/{database}"


def _load_apply_rls():
    path = Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location(f"_apply_rls_{id(path)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _drop(database: str, role: str) -> None:
    admin = await asyncpg.connect(admin_dsn(), timeout=20)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        await admin.close()


@asynccontextmanager
async def rls_database(monkeypatch, *, database: str, role: str, password: str):
    """Yields the owner URL; the app's ``database_url`` is the role, ``db_owner_url`` the owner."""
    from nexus.core import db as core_db
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    await _drop(database, role)
    admin = await asyncpg.connect(admin_dsn(), timeout=20)
    await admin.execute(f'CREATE DATABASE "{database}"')
    await admin.close()

    owner_url = PG_URL.rsplit("/", 1)[0] + "/" + database
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", owner_url)
    monkeypatch.setattr(settings, "db_owner_url", "")
    resolver.invalidate()
    await core_db.dispose_db()
    monkeypatch.setattr(core_db, "_platform_engine", None, raising=False)
    monkeypatch.setattr(core_db, "_platform_sessionmaker", None, raising=False)
    await core_db.init_db()
    await core_db.dispose_db()

    script = _load_apply_rls()
    monkeypatch.setattr(script, "APP_ROLE", role)
    monkeypatch.setenv("NEXUS_APP_DB_PASSWORD", password)
    monkeypatch.setenv("NEXUS_DB_OWNER_URL", owner_url)
    await script.main()

    monkeypatch.setattr(settings, "database_url",
                        role_dsn(database, role, password).replace(
                            "postgresql://", "postgresql+asyncpg://", 1))
    monkeypatch.setattr(settings, "db_owner_url", owner_url)
    resolver.invalidate()
    try:
        yield owner_url
    finally:
        await core_db.dispose_db()
        platform = core_db._platform_engine
        if platform is not None:
            await platform.dispose()
        await _drop(database, role)
        resolver.invalidate()


async def seed_billing() -> None:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


def app_client():
    import httpx

    from nexus.main import create_app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                             base_url="http://test")
