"""The three ledger stores, against the REAL Supabase projects (spec §18.2, §18.3, D21).

What only a live run can prove: that the connection strings in Provider keys reach a database, that
the role there owns `nexus_ledger` and can apply the schema, that Supabase's pooler tolerates the way
we connect (`statement_cache_size=0`), and that a round trip — insert, read back, delete — works
through it. The offline and integration suites prove everything else.

Skipped, loudly and by name, unless the DSNs are in the environment:

    NEXUS_LEDGER_ARCHIVE_DSN, NEXUS_LEDGER_TRAINING_DSN, NEXUS_LEDGER_INSIGHTS_DSN,
    NEXUS_LEDGER_PSEUDONYM_SECRET

These are the same values the Control plane stores; the environment is the documented fallback, so a
live run needs no database. **Nothing here writes a real event**: the round trip uses one synthetic
row with an id that names it, and deletes it again.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests_live.engagement.conftest import require_env

pytestmark = [pytest.mark.asyncio]

STORES = ("archive", "training", "insights")
PROBE_ID = "live-probe-0000000000000000"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Point the app at the live stores, or skip naming exactly what is missing."""
    from nexus.core.config import get_settings
    from nexus.providers import resolver

    values = require_env("NEXUS_LEDGER_ARCHIVE_DSN", "NEXUS_LEDGER_TRAINING_DSN",
                         "NEXUS_LEDGER_INSIGHTS_DSN", "NEXUS_LEDGER_PSEUDONYM_SECRET")
    settings = get_settings()
    monkeypatch.setattr(settings, "ledger_archive_dsn", values["NEXUS_LEDGER_ARCHIVE_DSN"])
    monkeypatch.setattr(settings, "ledger_training_dsn", values["NEXUS_LEDGER_TRAINING_DSN"])
    monkeypatch.setattr(settings, "ledger_insights_dsn", values["NEXUS_LEDGER_INSIGHTS_DSN"])
    monkeypatch.setattr(settings, "ledger_pseudonym_secret",
                        values["NEXUS_LEDGER_PSEUDONYM_SECRET"])
    resolver.invalidate()
    return values


async def test_every_store_is_reachable_and_owned_by_its_own_role():
    """The role must own `nexus_ledger` — that is what lets the Control plane apply the schema, and
    `check_store_dsn` refuses a superuser, so a project's `postgres` user will fail here."""
    from nexus.engagement.ledger import stores

    for store in STORES:
        report = await stores.status(store)
        assert report["reachable"], f"{store}: {report['detail']}"
        assert report["owns_schema"], f"{store}: {report['detail']}"


async def test_the_schema_is_applied_and_applying_it_again_changes_nothing():
    from nexus.engagement.ledger import schema
    from nexus.engagement.ledger.stores import connect

    for store in STORES:
        await schema.apply_schema(store)
        assert await schema.apply_schema(store) == [], f"{store} re-applied a version"
        async with connect(store) as conn:
            applied = await schema.applied_versions(conn)
        assert applied == {version for version, _sql in schema.migrations(store)}


async def test_a_sealed_event_round_trips_through_the_pooler(_configured):
    """One synthetic row, read back through a second connection, then deleted.

    The second connection is the point: Supabase's pooler may hand it a different server
    connection, which is why the app disables asyncpg's statement cache."""
    from nexus.engagement.ledger import envelope, schema, shipper
    from nexus.engagement.ledger.stores import connect

    await schema.apply_schema("archive")
    secret = _configured["NEXUS_LEDGER_PSEUDONYM_SECRET"]
    body = envelope.build(event_id=PROBE_ID, event_type="audit.action", tenant_id="live-probe",
                          occurred_at=NOW, payload={"action": "ledger.live_probe"})
    row = shipper.archive_row(secret, body, {"person_emails": []})
    try:
        async with connect("archive") as conn:
            await conn.execute(
                "INSERT INTO nexus_ledger.events "
                "(event_id, event_type, schema_version, occurred_at, tenant_id, sealed, "
                " person_keys) VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (event_id) DO NOTHING", *row)
        async with connect("archive") as conn:
            sealed = await conn.fetchval(
                "SELECT sealed FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID)
        assert sealed, "the probe row was not readable on a second connection"
        opened = shipper.open_document(secret, sealed)
        assert opened["envelope"]["payload"]["action"] == "ledger.live_probe"
    finally:
        async with connect("archive") as conn:
            await conn.execute("DELETE FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID)
        async with connect("archive") as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM nexus_ledger.events WHERE event_id = $1", PROBE_ID) == 0
