"""Prospector's tables and the Saved tab's SQL against REAL Postgres with Row-Level Security.

The offline suite runs on SQLite, which has no RLS and no JSON path operators, so three things only
a real server can prove:

* ``scripts/apply_rls.py`` enrols the two tenant tables (``prospector_purchases``,
  ``prospector_daily_rows``) and leaves the platform-global cache (``prospector_pages``) alone.
* Under the least-privilege app role a workspace sees and writes only its own ledger rows, and the
  shared cache stays readable with no tenant bound.
* The Saved tab's JSON-path filters (person city, company city, tech stack) compile and match on
  Postgres ``json`` columns, through a ``TenantSession`` bound by ``apply_rls``.

Skipped unless ``NEXUS_TEST_POSTGRES_URL`` is set. The script runs against a throwaway database with
a throwaway role name, never ``nexus_app``: ``apply_rls`` resets that role's password, and a test
must not do that to a server a real deploy might share. Both are dropped afterwards.
"""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from tests_integration.conftest import PG_URL, requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]

ROLE = "nexus_app_prospector_test"
PASSWORD = "prospector-rls-test-password"
DATABASE = "nexus_prospector_rls_test"
TENANT_A, TENANT_B = "t-prospector-a", "t-prospector-b"


def _admin_dsn(database: str = "") -> str:
    dsn = PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    return dsn.rsplit("/", 1)[0] + "/" + database if database else dsn


def _role_dsn() -> str:
    parsed = urlparse(_admin_dsn())
    return f"postgresql://{ROLE}:{PASSWORD}@{parsed.hostname}:{parsed.port or 5432}/{DATABASE}"


def _load_apply_rls():
    path = Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("_apply_rls_prospector", path)
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
async def rls_db(monkeypatch):
    """A fresh database with the whole schema, RLS applied, and the app pointed at the app role."""
    from nexus.core import db as core_db
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    await _drop()
    admin = await asyncpg.connect(_admin_dsn(), timeout=20)
    await admin.execute(f'CREATE DATABASE "{DATABASE}"')
    await admin.close()

    owner_url = PG_URL.rsplit("/", 1)[0] + "/" + DATABASE
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
    monkeypatch.setattr(script, "APP_ROLE", ROLE)
    monkeypatch.setenv("NEXUS_APP_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("NEXUS_DB_OWNER_URL", owner_url)
    await script.main()

    # From here the app's tenant sessions run as the least-privilege role; platform sessions
    # (seeding, the shared cache) as the owner, exactly as the API is deployed.
    monkeypatch.setattr(settings, "database_url",
                        _role_dsn().replace("postgresql://", "postgresql+asyncpg://", 1))
    monkeypatch.setattr(settings, "db_owner_url", owner_url)
    resolver.invalidate()

    yield owner_url

    await core_db.dispose_db()
    platform = core_db._platform_engine
    if platform is not None:
        await platform.dispose()
    await _drop()
    resolver.invalidate()


async def _as_role(tenant: str | None):
    conn = await asyncpg.connect(_role_dsn(), timeout=20)
    if tenant is not None:
        await conn.execute("SELECT set_config('app.current_tenant', $1, false)", tenant)
    return conn


async def test_only_the_two_tenant_tables_are_enrolled(rls_db):
    owner = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    try:
        rows = await owner.fetch(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY($1::text[])",
            ["prospector_pages", "prospector_purchases", "prospector_daily_rows"])
        policies = {r["tablename"] for r in await owner.fetch(
            "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'")}
    finally:
        await owner.close()

    enrolled = {r["relname"]: r["relrowsecurity"] for r in rows}
    assert enrolled == {"prospector_pages": False, "prospector_purchases": True,
                        "prospector_daily_rows": True}
    assert {"prospector_purchases", "prospector_daily_rows"} <= policies
    assert "prospector_pages" not in policies


async def test_a_workspace_sees_and_writes_only_its_own_ledger_rows(rls_db):
    insert = ("INSERT INTO prospector_purchases (id, tenant_id, fingerprint, field, request_id, "
              "first_charged_at, last_delivered_at, created_at, updated_at) "
              "VALUES ($1, $2, $3, 'email', 'r', now(), now(), now(), now())")
    a = await _as_role(TENANT_A)
    try:
        await a.execute(insert, "p-a", TENANT_A, "f" * 64)
        await a.execute("INSERT INTO prospector_daily_rows (id, tenant_id, day, rows, created_at, "
                        "updated_at) VALUES ('d-a', $1, '2026-10-19', 25, now(), now())", TENANT_A)
        # Writing for another workspace is refused by the policy's WITH CHECK.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await a.execute(insert, "p-b", TENANT_B, "e" * 64)
        assert await a.fetchval("SELECT count(*) FROM prospector_purchases") == 1
    finally:
        await a.close()

    b = await _as_role(TENANT_B)
    try:
        # Even a query with no WHERE clause returns nothing of A's.
        assert await b.fetchval("SELECT count(*) FROM prospector_purchases") == 0
        assert await b.fetchval("SELECT count(*) FROM prospector_daily_rows") == 0
        assert await b.execute("UPDATE prospector_daily_rows SET rows = 0") == "UPDATE 0"
    finally:
        await b.close()

    nobody = await _as_role(None)
    try:
        assert await nobody.fetchval("SELECT count(*) FROM prospector_purchases") == 0
    finally:
        await nobody.close()


async def test_the_shared_cache_needs_no_tenant(rls_db):
    from nexus.core.db import utcnow
    from nexus.prospector import cache
    from nexus.prospector.records import ParsedPage

    # Both calls swallow and log their failures, so a miss here is the failure being tested.
    await cache.put_page("k" * 64, view="contacts", page=1, size=25,
                         parsed=ParsedPage(items=[{"Email": "someone@example.com"}], total=1))
    hit = await cache.get_page("k" * 64)
    assert hit is not None and hit.items == [{"Email": "someone@example.com"}] and hit.total == 1

    # Sealed at rest, and readable by the app role with no tenant bound.
    role = await _as_role(None)
    try:
        row = await role.fetchrow("SELECT payload_sealed, expires_at FROM prospector_pages")
    finally:
        await role.close()
    assert "someone@example.com" not in row["payload_sealed"]
    assert row["expires_at"] > utcnow() + timedelta(hours=23)


async def _seed_workspaces() -> None:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account, Contact

    async with get_platform_sessionmaker()() as session:
        for tenant, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
            session.add(Account(id=f"acc-{suffix}", tenant_id=tenant, name="Northwind Example",
                                domain=f"northwind-{suffix}.example.com", country="United States",
                                employee_count=120, tech_stack=["Salesforce", "HubSpot"],
                                custom_fields={"city": "Austin"}))
            session.add(Contact(id=f"con-{suffix}", tenant_id=tenant, account_id=f"acc-{suffix}",
                                full_name="Ava Patel", title="VP Sales",
                                email=f"ava@northwind-{suffix}.example.com",
                                custom_fields={"location": {"city": "Denver", "country": "US"}}))
        session.add(Account(id="acc-other", tenant_id=TENANT_A, name="Bluepeak Example",
                            tech_stack=["Zendesk"], custom_fields={"city": "Boston"}))
        await session.commit()


async def _saved(view: str, **filters):
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.prospector.filters import SearchFilters, WorkspaceFilters
    from nexus.prospector.saved import saved_page

    async with get_sessionmaker()() as session:
        await apply_rls(session, TENANT_A)
        ts = TenantSession(session, TENANT_A)
        return await saved_page(ts, view=view, filters=SearchFilters(**filters),
                                workspace=WorkspaceFilters(), page=1, page_size=25)


async def test_the_saved_tabs_json_filters_run_on_postgres_under_rls(rls_db):
    await _seed_workspaces()

    people = await _saved("contacts", person_cities=["denver"], company_cities=["Austin"],
                          technologies=["salesforce"], titles=["sales"])
    assert [(r.id, r.domain) for r in people.rows] == [("con-a", "northwind-a.example.com")]
    assert people.total == 1

    companies = await _saved("companies", technologies=["HubSpot"], employee_sizes=["51-200"])
    assert [r.id for r in companies.rows] == ["acc-a"]

    assert (await _saved("companies", company_cities=["Boston"])).total == 1
    assert (await _saved("contacts", person_cities=["Paris"])).total == 0


async def test_a_search_releases_its_connection_and_still_reads_as_the_tenant(rls_db):
    """The provider wait no longer holds a connection 'idle in transaction' (measured 2026-10-22),
    and the commit that releases it does not lose the tenant: ``apply_rls`` is transaction-local,
    so without the re-bind the held contact below would read as new and the day's count would be
    refused by the policy's WITH CHECK."""
    import httpx

    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient
    from nexus.prospector.limits import rows_today
    from nexus.prospector.schemas import SearchIn

    await _seed_workspaces()
    items = [{"FirstName": "Ava", "LastName": "Patel", "Email": "ava@northwind-a.example.com",
              "CompanyName": "Northwind Example", "CompanyURL": "northwind-a.example.com"},
             {"FirstName": "Leo", "LastName": "Grant", "Email": "leo@fresh-example.com",
              "CompanyName": "Fresh Example", "CompanyURL": "fresh-example.com"}]
    seen: list[bool] = []

    def vendor(request):
        return httpx.Response(200, json={"PageNumber": 1, "PageSize": 25, "Items": items})

    class Watching(InfoJoyClient):
        async def search(self, *a, **kw):
            seen.append(ts.session.in_transaction())
            return await super().search(*a, **kw)

    search.set_client(Watching(keys=["k"], transport=httpx.MockTransport(vendor), backoff_s=0))
    try:
        async with get_sessionmaker()() as session:
            await apply_rls(session, TENANT_A)
            ts = TenantSession(session, TENANT_A)
            out = await search.search(ts, user_id="u", body=SearchIn(view="contacts"))
            counted = await rows_today(ts)
            await session.commit()
    finally:
        search.set_client(None)

    assert seen == [False]
    assert [r.saved for r in out.rows] == [True, False]
    assert counted == 2
    owner = await asyncpg.connect(_admin_dsn(DATABASE), timeout=20)
    try:
        assert await owner.fetchval(
            "SELECT tenant_id FROM prospector_daily_rows") == TENANT_A
    finally:
        await owner.close()
