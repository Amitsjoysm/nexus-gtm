"""The ledger stores against REAL Postgres: schema, shipping, building, deletion (spec §18.2-§18.6).

The offline suite proves the pure halves. Everything here is the part a fake cannot prove: that the
versioned SQL applies and is idempotent, that the upserts converge when a batch is replayed, that
`jsonb` label merging keeps what a later event learned, that `text[]` person keys can be searched,
and that a deletion actually leaves zero rows.

Skipped unless `NEXUS_TEST_POSTGRES_URL` is set, exactly like the other integration tests. The CI
leg provides a superuser connection; this module creates its own role and four databases inside it —
three stores plus an app database — and drops them afterwards, so it leaves the server as it found
it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from tests_integration.conftest import PG_URL, requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]

SECRET = "an-integration-pseudonymisation-secret-0123456789abcdef"
ROLE = "nexus_ledger_test"
PASSWORD = "ledger-test-password"
DATABASES = {"archive": "nexus_ledger_archive_test", "training": "nexus_ledger_training_test",
             "insights": "nexus_ledger_insights_test"}
APP_DB = "nexus_ledger_app_test"
TENANT = "t-integration"
NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)


def _admin_dsn() -> str:
    return PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _store_dsn(database: str) -> str:
    parsed = urlparse(_admin_dsn())
    return f"postgresql://{ROLE}:{PASSWORD}@{parsed.hostname}:{parsed.port or 5432}/{database}"


async def _admin(database: str = ""):
    dsn = _admin_dsn()
    if database:
        dsn = dsn.rsplit("/", 1)[0] + "/" + database
    return await asyncpg.connect(dsn, timeout=20)


@pytest_asyncio.fixture
async def stores(monkeypatch):
    """A role that owns `nexus_ledger` in three databases, plus an app database, torn down after."""
    from nexus.core import db as core_db
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    admin = await _admin()
    await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await admin.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'CREATE DATABASE "{database}" OWNER {ROLE}')
    await admin.close()

    for database in DATABASES.values():
        conn = await _admin(database)
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS nexus_ledger AUTHORIZATION {ROLE}")
        await conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "ledger_pseudonym_secret", SECRET)
    monkeypatch.setattr(settings, "source_db_allow_private", True)
    for store, database in DATABASES.items():
        monkeypatch.setattr(settings, f"ledger_{store}_dsn", _store_dsn(database))
    # The app database moves with us, so the shipper's outbox reads hit real Postgres too.
    monkeypatch.setattr(settings, "database_url",
                        _store_dsn(APP_DB).replace("postgresql://", "postgresql+asyncpg://", 1))
    monkeypatch.setattr(settings, "db_owner_url", "")
    resolver.invalidate()
    await core_db.dispose_db()
    monkeypatch.setattr(core_db, "_platform_engine", None, raising=False)
    monkeypatch.setattr(core_db, "_platform_sessionmaker", None, raising=False)

    # The whole app schema, not just the ledger tables: the provider-key resolver reads
    # `provider_keys` on every secret lookup, and a missing table there is not the failure this
    # module is testing.
    await core_db.init_db()

    yield

    await core_db.dispose_db()
    admin = await _admin()
    for database in (*DATABASES.values(), APP_DB):
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    await admin.close()
    resolver.invalidate()


async def _apply_all() -> None:
    from nexus.engagement.ledger import schema

    for store in DATABASES:
        await schema.apply_schema(store)


async def _outbox(event_type: str, payload: dict, refs: dict, event_id: str) -> None:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import envelope
    from nexus.models.ledger import LedgerOutbox

    body = envelope.build(event_id=event_id, event_type=event_type, tenant_id=TENANT,
                          occurred_at=NOW, refs=refs, payload=payload)
    async with get_platform_sessionmaker()() as session:
        session.add(LedgerOutbox(tenant_id=TENANT, event_id=event_id, event_type=event_type,
                                 schema_version=1, occurred_at=NOW, payload=body))
        await session.commit()


async def _seed_records() -> None:
    """A tenant, an account and a contact in the app database, so the shipper resolves a real
    person — which is what turns a sent message into an insights fact."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account, Contact
    from nexus.models.identity import Tenant

    async with get_platform_sessionmaker()() as session:
        session.add(Tenant(id=TENANT, name="Integration", slug="integration"))
        session.add(Account(id="a-1", tenant_id=TENANT, name="Acme Robotics", domain="acme.io"))
        session.add(Contact(id="c-1", tenant_id=TENANT, account_id="a-1", full_name="Jane Buyer",
                            email="jane.buyer@acme.io", title="VP Engineering"))
        await session.commit()


async def _count(store: str, sql: str, *args) -> int:
    from nexus.engagement.ledger.stores import connect

    async with connect(store) as conn:
        return int(await conn.fetchval(sql, *args) or 0)


async def test_the_schema_applies_once_and_is_safe_to_apply_again(stores):
    from nexus.engagement.ledger import schema, stores as store_module

    for store in DATABASES:
        applied = await schema.apply_schema(store)
        assert applied == ["0001_initial"], store
        assert await schema.apply_schema(store) == [], f"{store} re-applied a version"
        status = await store_module.status(store)
        assert status["reachable"] and status["owns_schema"], status
        assert status["applied"] == ["0001_initial"] and status["pending"] == []


async def test_a_batch_ships_seals_and_can_be_shipped_again_without_duplicating(stores):
    from nexus.engagement.ledger import shipper

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi Jane Buyer at Acme"},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-1")
    result = await shipper.ship()
    assert result["shipped"] == 1

    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "archive") as conn:
        rows = await conn.fetch("SELECT event_id, tenant_id, sealed FROM nexus_ledger.events")
    assert [row["event_id"] for row in rows] == ["ev-1"]
    assert "Jane" not in rows[0]["sealed"]
    opened = shipper.open_document(SECRET, rows[0]["sealed"])
    assert opened["envelope"]["payload"]["body"] == "Hi Jane Buyer at Acme"

    # Shipping again must not duplicate: the row is marked, and the insert ignores a repeat.
    assert (await shipper.ship())["shipped"] == 0
    assert await _count("archive", "SELECT count(*) FROM nexus_ledger.events") == 1


async def test_building_produces_examples_facts_and_a_profile_and_a_replay_keeps_the_labels(stores):
    from nexus.engagement.ledger import builder, datasets, shipper

    await _apply_all()
    await _seed_records()
    await _outbox("message.sent",
                  {"subject": "Quick question", "body": "Hi Jane", "context_pack": "Acme ...",
                   "sent_local_hour": 9, "sent_local_weekday": 1, "step_index": 0},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-send")
    await _outbox("reply.received",
                  {"inbound_kind": "human", "response_latency_s": 3600, "local_hour": 10,
                   "local_weekday": 1, "body": "Interesting"},
                  {"message_id": "m-2", "answered_message_id": "m-1",
                   "contact_id": "c-1"}, "ev-reply")
    await _outbox("reply.classified",
                  {"category": "interested", "confidence": 0.9, "body": "Interesting"},
                  {"message_id": "m-2", "answered_message_id": "m-1",
                   "contact_id": "c-1"}, "ev-class")
    await shipper.ship()

    built = await builder.build()
    assert built["built"] == 3

    example_id = datasets.example_id("sft_outreach_email", "m-1")
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        example = await conn.fetchrow(
            "SELECT split, record, consent_terms_version FROM nexus_ledger.examples "
            "WHERE example_id = $1", example_id)
        events = await conn.fetchval("SELECT count(*) FROM nexus_ledger.events")
    import json

    record = json.loads(example["record"])
    assert record["labels"] == {"replied": True, "positive": True, "meeting": False,
                                "response_latency_s": 3600}
    assert example["split"] in ("train", "val", "test")
    assert events == 3

    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "insights") as conn:
        facts = await conn.fetch("SELECT fact_type, category FROM nexus_ledger.facts "
                                 "ORDER BY fact_type")
        profile = await conn.fetchrow("SELECT * FROM nexus_ledger.person_profiles")
    assert [(f["fact_type"], f["category"]) for f in facts] == [("reply", "interested"),
                                                                ("send", "")]
    assert profile["sends"] == 1 and profile["replies"] == 1
    assert profile["last_reply_band"] == "within_hour"
    assert profile["workspace_count"] == 1

    # Replaying the whole archive (what a scrubber fix does) must not un-learn the reply label.
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        await conn.execute("DELETE FROM nexus_ledger.build_state")
    await builder.build()
    async with __import__("nexus.engagement.ledger.stores", fromlist=["x"]).connect(
            "training") as conn:
        again = json.loads(await conn.fetchval(
            "SELECT record FROM nexus_ledger.examples WHERE example_id = $1", example_id))
    assert again["labels"]["replied"] is True and again["labels"]["positive"] is True


async def test_a_workspace_that_opts_out_is_removed_from_every_store(stores):
    from nexus.engagement.ledger import builder, deletion, shipper

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"}, {"message_id": "m-1"}, "ev-1")
    await shipper.ship()
    await builder.build()

    report = await deletion.delete_workspace(TENANT)
    assert report["remaining"] == {"outbox": 0, "archive": 0, "training": 0, "insights": 0}, report
    assert await _count("archive", "SELECT count(*) FROM nexus_ledger.events") == 0
    assert await _count("training", "SELECT count(*) FROM nexus_ledger.examples") == 0
    # Idempotent: asking twice reports zero remaining, not an error.
    assert (await deletion.delete_workspace(TENANT))["remaining"]["archive"] == 0


async def test_erasing_a_person_takes_their_rows_and_leaves_the_company_profile_honest(stores):
    from nexus.engagement.ledger import builder, deletion, shipper
    from nexus.engagement.ledger.pseudonym import person_key
    from nexus.engagement.ledger.stores import connect

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"},
                  {"message_id": "m-1", "contact_id": "c-1"}, "ev-1")
    await shipper.ship()
    # The shipper resolves against the app database, which holds no contacts here, so the fact is
    # written by hand with the same key the erasure will search for.
    key = person_key(SECRET, "jane.buyer@acme.io")
    async with connect("insights") as conn:
        await conn.execute(
            "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key, "
            " company_domain, workspace_key, occurred_at) "
            "VALUES ('f-1', 'send', 'jane.buyer@acme.io', $1, 'acme.io', 'w-1', now())", key)
        await conn.execute(
            "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key, "
            " company_domain, workspace_key, occurred_at) "
            "VALUES ('f-2', 'send', 'other@acme.io', 'other-key', 'acme.io', 'w-1', now())")
    async with connect("insights") as conn:
        await builder.refresh_person(conn, "jane.buyer@acme.io")
        await builder.refresh_company(conn, "acme.io")
        assert await conn.fetchval(
            "SELECT sends FROM nexus_ledger.company_profiles WHERE company_domain = 'acme.io'") == 2

    report = await deletion.erase_person(key)
    assert report["remaining"]["insights"] == 0, report
    async with connect("insights") as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM nexus_ledger.facts WHERE person_key = $1", key) == 0
        # The colleague's fact stays, and the company profile now counts only what is left.
        assert await conn.fetchval(
            "SELECT sends FROM nexus_ledger.company_profiles WHERE company_domain = 'acme.io'") == 1


async def test_a_store_that_refuses_leaves_the_rows_to_retry_with_backoff(stores):
    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import shipper
    from nexus.engagement.ledger.stores import StoreUnavailable
    from nexus.models.ledger import LedgerOutbox
    from nexus.providers import resolver
    from sqlalchemy import select

    await _apply_all()
    await _outbox("message.sent", {"subject": "Hi", "body": "Hi"}, {"message_id": "m-1"}, "ev-1")
    # Point the archive at a database that does not exist: a real refusal, not a patched function.
    get_settings().ledger_archive_dsn = _store_dsn("nexus_ledger_absent_test")
    resolver.invalidate()
    with pytest.raises(StoreUnavailable):
        await shipper.ship_batch()

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(select(LedgerOutbox))).scalars().one()
    assert row.shipped_archive_at is None and row.attempts == 1 and row.last_error
    assert shipper.is_due(row.attempts, row.updated_at, row.updated_at + timedelta(minutes=1)) \
        is False


async def test_the_insights_client_reads_profiles_without_their_workspace_keys(stores):
    """Phase 13: the app reads profiles back from the real store; the key list stays in the store,
    a miss is remembered, and an unconfigured store is simply no answer."""
    from nexus.core.config import get_settings
    from nexus.engagement.insights import client
    from nexus.engagement.ledger import stores as ledger_stores
    from nexus.providers import resolver

    await _apply_all()
    client.clear_cache()
    async with ledger_stores.connect("insights") as conn:
        await conn.execute(
            "INSERT INTO nexus_ledger.person_profiles (person_email, person_key, company_domain,"
            " best_weekday, best_hour, median_response_s, reply_propensity, last_reply_band,"
            " sends, replies, workspace_count, workspace_keys) VALUES ('jane@acme.io', 'pk',"
            " 'acme.io', 1, 10, 14400, 0.2, 'same_day', 10, 2, 3, ARRAY['w1','w2','w3'])")

    found = await client.person_profiles(["Jane@Acme.io", "nobody@acme.io"])
    assert list(found) == ["jane@acme.io"]
    assert found["jane@acme.io"]["workspace_count"] == 3
    assert "workspace_keys" not in found["jane@acme.io"]

    # Remembered for a few minutes, the miss included: no second query for either person.
    async with ledger_stores.connect("insights") as conn:
        await conn.execute("DELETE FROM nexus_ledger.person_profiles")
    assert list(await client.person_profiles(["jane@acme.io", "nobody@acme.io"])) == ["jane@acme.io"]
    client.clear_cache()
    assert await client.person_profiles(["jane@acme.io"]) == {}

    get_settings().ledger_insights_dsn = ""
    resolver.invalidate()
    client.clear_cache()
    assert await client.person_profiles(["jane@acme.io"]) == {}
