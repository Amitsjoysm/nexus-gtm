# tests/test_source_databases.py
"""Registered external source databases: the staging ladder and what it refuses.

The subject of these tests is not "does it connect" — it is **can a source become consumable
without having been proved**. Every rung exists because this subsystem has shipped six
wrong-attribution bugs, and the dry run is the only thing standing between a plausible-looking
mapping and wrong facts written into the SHARED company/person stores, wrong for every tenant at
once.

Nothing here reaches a real database. The engine functions are the seam that talks to a foreign
host; the ladder, the guards and the state machine are what these tests pin.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _as_admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


def _allow_private(monkeypatch):
    """Local DSNs are refused by default; these tests are about the ladder, not the SSRF guard."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "source_db_allow_private", True)


# A distinctive password, because every "the secret did not leak" assertion in this file is a
# substring search for it. A two-character one (`pw`) made those assertions unreliable in BOTH
# directions: it turns up inside Fernet's base64 output by pure chance about 4% of the time, which
# failed the round-trip test at random, and it could equally hide inside an unrelated word in a
# response body and never be noticed. A hit has to mean a leak, or the assertion is decoration.
PASSWORD = "s3cr3t-pw-9f2a1c"
DSN = f"postgresql://user:{PASSWORD}@db.example.com:5432/warehouse"

DISCOVERED = {
    "tables": [
        {
            "schema": "public",
            "table": "accounts",
            "columns": [
                {"name": "website", "type": "text"},
                {"name": "legal_name", "type": "text"},
                {"name": "headcount", "type": "integer"},
                {"name": "notes", "type": "text"},
            ],
        }
    ]
}

MAPPING = {
    "entity": "company",
    "schema": "public",
    "table": "accounts",
    "columns": {"domain": "website", "name": "legal_name", "employee_count": "headcount"},
}


# ---- the secret ---------------------------------------------------------------------------

async def test_the_dsn_is_sealed_at_rest_and_round_trips(fresh_db):
    """A DSN is a live credential to somebody else's database. It never sits in a column
    in the clear."""
    from nexus.sources.crypto import seal_dsn, unseal_dsn

    sealed = seal_dsn(DSN)
    assert DSN not in sealed
    assert PASSWORD not in sealed
    assert unseal_dsn(sealed) == DSN


async def test_an_unsealable_dsn_raises_rather_than_reading_as_absent(fresh_db):
    """`""` is what a DELETED secret looks like. If a key rotation made a stored DSN
    unreadable and we returned empty, the caller would report 'not configured' — and the
    operator's next move for those two states is opposite."""
    from nexus.sources.crypto import DsnUnsealable, unseal_dsn

    with pytest.raises(DsnUnsealable):
        unseal_dsn("not-a-fernet-token")


async def test_the_stored_redaction_carries_no_password(fresh_db, monkeypatch):
    _allow_private(monkeypatch)
    from nexus.sources import service

    row = await service.register(name="wh", dsn=DSN)
    assert PASSWORD not in row.dsn_redacted
    assert "db.example.com" in row.dsn_redacted


# ---- the ladder ---------------------------------------------------------------------------

async def test_a_new_source_is_not_usable(fresh_db, monkeypatch):
    """Registered is the bottom rung, and `enabled` starts false: an operator must get to look
    at the dry run before anything can consume the source."""
    from nexus.sources import service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    assert row.status == "registered"
    assert row.enabled is False
    assert row.is_usable() is False
    assert await service.usable_sources() == []


async def test_enabling_is_refused_until_a_dry_run_has_passed(fresh_db, monkeypatch):
    """This is what makes the dry run a gate rather than a suggestion."""
    from nexus.sources import service
    from nexus.sources.safety import SourceRejected

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    with pytest.raises(SourceRejected, match="dry run"):
        await service.set_enabled(row.id, True)


async def test_disabling_is_never_refused(fresh_db, monkeypatch):
    """During an incident, 'stop reading this' must not be blocked by a state machine."""
    from nexus.sources import service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    off = await service.set_enabled(row.id, False)
    assert off.enabled is False


async def test_mapping_is_refused_before_introspection(fresh_db, monkeypatch):
    from nexus.sources import service
    from nexus.sources.safety import SourceRejected

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    with pytest.raises(SourceRejected, match="introspect"):
        await service.set_mapping(row.id, MAPPING)


async def test_a_verified_source_is_usable_only_while_enabled(fresh_db, monkeypatch):
    """Both conditions, and no third. An operator who disabled a source has said 'do not read
    this'; a provider testing `status` alone would keep reading it."""
    from nexus.models.source_db import SourceDatabase
    from nexus.core.db import get_platform_sessionmaker
    from nexus.sources import service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.status = "verified"
        live.enabled = True
        await s.commit()
    assert len(await service.usable_sources()) == 1

    await service.set_enabled(row.id, False)
    assert await service.usable_sources() == []


async def test_re_introspection_invalidates_the_proof(fresh_db, monkeypatch):
    """A mapping is only meaningful against the schema it was validated against. A source that
    stays `verified` after its source table was rebuilt is the wrong-attribution bug."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.source_db import SourceDatabase
    from nexus.sources import engine, service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.discovered_schema = DISCOVERED
        live.status = "introspected"
        await s.commit()
    await service.set_mapping(row.id, MAPPING)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.status = "verified"
        live.dry_run = {"rows": 5, "usable_rows": 5, "verified": True}
        await s.commit()

    async def _fake_introspect(sealed, **kw):
        return [
            engine.TableInfo(
                schema="public", table="accounts",
                columns=[engine.ColumnInfo(name="website", data_type="text")],
            )
        ]

    monkeypatch.setattr(engine, "introspect", _fake_introspect)
    await service.introspect(row.id)

    after = await service.get_source(row.id)
    assert after.status == "introspected"
    assert after.dry_run == {}
    assert after.is_usable() is False


async def test_a_dry_run_that_finds_no_identities_does_not_verify(fresh_db, monkeypatch):
    """Rows alone say the query ran. A mapping pointed at the wrong column returns a full page
    of rows with no domain, and looks like success if you only count rows."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.source_db import SourceDatabase
    from nexus.sources import engine, service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.discovered_schema = DISCOVERED
        live.status = "introspected"
        await s.commit()
    await service.set_mapping(row.id, MAPPING)

    async def _empty_identities(sealed, mapping, **kw):
        return {"entity": "company", "rows": 25, "usable_rows": 0,
                "identity_field": "domain", "sample": [], "statement": "SELECT ..."}

    monkeypatch.setattr(engine, "dry_run", _empty_identities)
    result = await service.run_dry_run(row.id)

    assert result["verified"] is False
    after = await service.get_source(row.id)
    # Not `failed`: the source is reachable and the query ran. Saying "failed" would send an
    # operator to check the network instead of the columns.
    assert after.status == "mapped"
    assert "wrong column" in after.last_error
    assert after.is_usable() is False


async def test_a_passing_dry_run_verifies_but_does_not_switch_the_source_on(
    fresh_db, monkeypatch
):
    """Verification and activation are separate acts. Auto-enabling would remove the operator's
    chance to read the dry-run output before anything consumes the source."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.source_db import SourceDatabase
    from nexus.sources import engine, service

    _allow_private(monkeypatch)
    row = await service.register(name="wh", dsn=DSN)
    async with get_platform_sessionmaker()() as s:
        live = await s.get(SourceDatabase, row.id)
        live.discovered_schema = DISCOVERED
        live.status = "introspected"
        await s.commit()
    await service.set_mapping(row.id, MAPPING)

    async def _good(sealed, mapping, **kw):
        return {"entity": "company", "rows": 10, "usable_rows": 10,
                "identity_field": "domain", "sample": [], "statement": "SELECT ..."}

    monkeypatch.setattr(engine, "dry_run", _good)
    await service.run_dry_run(row.id)

    after = await service.get_source(row.id)
    assert after.status == "verified"
    assert after.enabled is False          # still off
    assert after.is_usable() is False
    assert await service.usable_sources() == []

    await service.set_enabled(row.id, True)
    assert len(await service.usable_sources()) == 1


# ---- mapping validation -------------------------------------------------------------------

def test_a_mapping_onto_an_undiscovered_column_is_refused():
    """Validity is not enough. A syntactically fine column we never saw means the admin is
    mapping onto something introspection did not return."""
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    bad = dict(MAPPING, columns={"domain": "not_a_real_column"})
    with pytest.raises(SourceRejected, match="not in public.accounts"):
        validate_mapping(bad, DISCOVERED)


def test_a_mapping_onto_an_undiscovered_table_is_refused():
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    with pytest.raises(SourceRejected, match="not found by introspection"):
        validate_mapping(dict(MAPPING, table="secrets"), DISCOVERED)


def test_a_company_mapping_without_a_domain_is_refused():
    """`companies.domain` is the identity key. A name match across tenants is exactly how this
    subsystem shipped six wrong-attribution bugs."""
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    with pytest.raises(SourceRejected, match="required field"):
        validate_mapping(dict(MAPPING, columns={"name": "legal_name"}), DISCOVERED)


def test_a_person_mapping_keyed_only_on_a_name_is_refused():
    """Getting a person wrong means a rep phones a stranger with someone else's context."""
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    discovered = {"tables": [{"schema": "public", "table": "people", "columns": [
        {"name": "fullname", "type": "text"}, {"name": "jobtitle", "type": "text"}]}]}
    mapping = {"entity": "person", "schema": "public", "table": "people",
               "columns": {"full_name": "fullname", "title": "jobtitle"}}
    with pytest.raises(SourceRejected, match="not an identity"):
        validate_mapping(mapping, discovered)


def test_an_unknown_app_field_is_refused_rather_than_dropped():
    """A typo in the console must surface as an error, not as a column that silently never
    populates."""
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    bad = dict(MAPPING, columns={"domain": "website", "revenue": "headcount"})
    with pytest.raises(SourceRejected, match="unknown company field"):
        validate_mapping(bad, DISCOVERED)


def test_an_unknown_entity_is_refused():
    from nexus.sources.engine import validate_mapping
    from nexus.sources.safety import SourceRejected

    with pytest.raises(SourceRejected, match="unknown entity"):
        validate_mapping(dict(MAPPING, entity="invoice"), DISCOVERED)


# ---- query building -----------------------------------------------------------------------

def test_build_select_revalidates_identifiers_it_was_handed():
    """'It was safe when we stored it' is exactly the assumption that makes stored-value
    injection work. This function is what puts a name into a SQL string, so it checks there."""
    from nexus.sources.engine import build_select
    from nexus.sources.safety import SourceRejected

    poisoned = {"entity": "company", "schema": "public", "table": 'x"; DROP TABLE y; --',
                "columns": {"domain": "website"}}
    with pytest.raises(SourceRejected, match="unsafe table"):
        build_select(poisoned, limit=5)

    poisoned_col = dict(MAPPING, columns={"domain": 'w"; DELETE FROM z; --'})
    with pytest.raises(SourceRejected, match="unsafe column"):
        build_select(poisoned_col, limit=5)


def test_build_select_binds_the_limit_rather_than_formatting_it():
    from nexus.sources.engine import build_select

    sql = build_select(MAPPING, limit=25)
    assert ":limit" in sql
    assert "LIMIT 25" not in sql


def test_build_select_quotes_every_identifier():
    from nexus.sources.engine import build_select

    sql = build_select(MAPPING, limit=5)
    assert '"public"."accounts"' in sql
    assert '"website" AS "domain"' in sql


# ---- the API gate -------------------------------------------------------------------------

async def test_the_console_never_returns_the_connection_string(client, monkeypatch):
    """A 'show connection string' affordance would turn every read of this console into a
    credential disclosure."""
    _allow_private(monkeypatch)
    token = await _as_admin(client, monkeypatch, "sdb1")
    r = await client.post("/api/admin/sources", headers=auth(token),
                          json={"name": "wh", "dsn": DSN})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "dsn" not in body
    assert "dsn_encrypted" not in body
    assert PASSWORD not in r.text
    assert body["usable"] is False


async def test_a_workspace_owner_cannot_reach_the_source_console(client):
    """Platform admin is a separate authorization system; no tenant role grants it."""
    token = await signup(client, slug="sdb2", email="owner@sdb2.com", company="SDB2")
    r = await client.get("/api/admin/sources", headers=auth(token))
    assert r.status_code in (401, 403)


async def test_registering_a_private_dsn_is_refused_by_default(client, monkeypatch):
    """The SSRF guard is the reason a DSN form is safe to expose at all."""
    token = await _as_admin(client, monkeypatch, "sdb3")
    r = await client.post(
        "/api/admin/sources", headers=auth(token),
        json={"name": "local", "dsn": "postgresql://u:p@127.0.0.1:5432/db"},
    )
    assert r.status_code == 400
    assert "refusing to connect" in r.text


async def test_a_non_postgres_scheme_is_refused(client, monkeypatch):
    token = await _as_admin(client, monkeypatch, "sdb4")
    r = await client.post("/api/admin/sources", headers=auth(token),
                          json={"name": "weird", "dsn": "mysql://u:p@db.example.com/x"})
    assert r.status_code == 400


async def test_duplicate_names_are_refused(client, monkeypatch):
    _allow_private(monkeypatch)
    token = await _as_admin(client, monkeypatch, "sdb5")
    first = await client.post("/api/admin/sources", headers=auth(token),
                              json={"name": "wh", "dsn": DSN})
    assert first.status_code == 201
    again = await client.post("/api/admin/sources", headers=auth(token),
                              json={"name": "wh", "dsn": DSN})
    assert again.status_code == 400


async def test_registration_is_audited(client, monkeypatch, fresh_db):
    """Every platform-admin mutation is answerable: who did this, when, and what was it before."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    _allow_private(monkeypatch)
    token = await _as_admin(client, monkeypatch, "sdb6")
    await client.post("/api/admin/sources", headers=auth(token),
                      json={"name": "wh", "dsn": DSN})

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(
            select(BillingAuditLog).where(
                BillingAuditLog.action == "source_database.register"
            )
        )).all()
    assert len(rows) == 1
    # The audit row must not become a new home for the secret.
    assert PASSWORD not in str(rows[0].after)


# ---- permission separation ------------------------------------------------------------------

def test_sources_manage_is_not_granted_by_the_support_or_finance_presets():
    """Registering a data source and granting platform power are different acts. A source
    mapped onto the wrong column writes wrong facts into the shared stores for every tenant."""
    from nexus.billing.permissions import ROLE_PRESETS, SOURCES_MANAGE

    assert SOURCES_MANAGE in ROLE_PRESETS["superadmin"]
    assert SOURCES_MANAGE not in ROLE_PRESETS["support"]
    assert SOURCES_MANAGE not in ROLE_PRESETS["finance"]


def test_the_ssrf_guard_cannot_be_switched_off_in_production():
    """`source_db_allow_private` is a local-development affordance. Silently ignoring it in prod
    would leave an operator believing the guard is off when it is on, or the reverse."""
    from nexus.core.config import Settings

    with pytest.raises(ValueError, match="SOURCE_DB_ALLOW_PRIVATE"):
        Settings(env="prod", secret_key="x" * 48, source_db_allow_private=True)

    # Local development is exactly where a source database on localhost is plausible.
    assert Settings(env="local", source_db_allow_private=True).source_db_allow_private is True
