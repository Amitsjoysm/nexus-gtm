# nexus/sources/service.py
"""The staging ladder for a registered source database.

    registered -> connected -> introspected -> mapped -> verified

Each function here does one rung and is the ONLY thing that advances ``status``. A request body
never carries a status: an admin who could set ``verified`` directly could skip the dry run, and
the dry run is the entire safety argument for this subsystem.

Rungs are re-runnable and are not a ratchet in the other direction. Re-introspecting a source that
was already ``verified`` drops it back to ``introspected`` and clears the mapping's proof, because
the schema it was verified against may have changed underneath us. A source that silently stays
"verified" after its source table was rebuilt is precisely the wrong-attribution failure this
subsystem keeps shipping.

Everything runs through ``get_platform_sessionmaker()``: ``source_databases`` has no ``tenant_id``,
so a TenantSession would be the wrong scope and, under RLS, would return zero rows rather than
error.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.source_db import SourceDatabase, USABLE_STATUS
from nexus.sources import engine
from nexus.sources.crypto import seal_dsn
from nexus.sources.safety import SourceRejected, redact_dsn, validate_dsn

logger = logging.getLogger("nexus.sources.service")


async def _load(session, source_id: str) -> SourceDatabase:
    row = await session.get(SourceDatabase, source_id)
    if row is None:
        raise LookupError(f"source database {source_id!r} not found")
    return row


async def _record(
    session, row: SourceDatabase, *, ok: bool, error: str = "", set_failed: bool = True
) -> None:
    """Stamp the outcome of a rung.

    ``last_error`` is cleared on success rather than left behind: a stale error next to a working
    source sends an operator chasing a problem that is already fixed, which is its own outage.

    ``set_failed=False`` records the error WITHOUT moving the source to ``failed``. That
    distinction is the point of the "dry run found no identities" case: the source is reachable
    and the query ran, so calling it ``failed`` sends an operator to check the network when the
    thing to check is the column mapping.
    """
    if ok:
        row.last_ok_at = utcnow()
        row.last_error = ""
    else:
        row.last_error = error[:2000]
        if set_failed:
            row.status = "failed"
    await session.commit()


async def register(*, name: str, dsn: str, kind: str = "postgres") -> SourceDatabase:
    """Create a source. Validates the DSN before it is ever stored, let alone connected to.

    Validating at registration rather than only at connect time means a DSN that points somewhere
    we refuse never reaches the database at all — there is no row to leak it from later.
    """
    from nexus.core.config import get_settings

    validate_dsn(dsn, allow_private=get_settings().source_db_allow_private)
    async with get_platform_sessionmaker()() as session:
        existing = (
            await session.scalars(select(SourceDatabase).where(SourceDatabase.name == name))
        ).first()
        if existing is not None:
            raise SourceRejected(f"a source database named {name!r} already exists")
        row = SourceDatabase(
            name=name,
            kind=kind,
            dsn_encrypted=seal_dsn(dsn),
            dsn_redacted=redact_dsn(dsn),
            status="registered",
            # Never on at creation. See the migration's note: an operator must get to look at the
            # dry run before anything can consume the source.
            enabled=False,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def connect(source_id: str) -> dict:
    """Rung 1 — prove we can reach it and that it is read-only for us."""
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        try:
            info = await engine.test_connection(row.dsn_encrypted)
        except Exception as exc:
            await _record(session, row, ok=False, error=str(exc))
            raise
        # Only ever advance from below. A verified source that still connects has not become less
        # proven, and knocking it back to `connected` would take it out of service for a health
        # check that passed.
        if row.status in ("registered", "failed"):
            row.status = "connected"
        await _record(session, row, ok=True)
        return info


async def introspect(source_id: str) -> dict:
    """Rung 2 — discover tables/columns and store them.

    Drops the source back to ``introspected`` and clears any dry-run proof, because a mapping is
    only meaningful against the schema it was validated against.
    """
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        try:
            tables = await engine.introspect(row.dsn_encrypted)
        except Exception as exc:
            await _record(session, row, ok=False, error=str(exc))
            raise
        row.discovered_schema = {"tables": [t.as_dict() for t in tables]}
        row.status = "introspected"
        # The proof is now stale by construction. Keeping it would let a source stay `verified`
        # against a schema that no longer exists.
        row.dry_run = {}
        if row.mapping:
            logger.info("source %s re-introspected; existing mapping must be re-verified", row.id)
        await _record(session, row, ok=True)
        return row.discovered_schema


async def set_mapping(source_id: str, mapping: dict) -> dict:
    """Rung 3 — validate the admin's mapping against what introspection found, and store it.

    Storing an *unvalidated* mapping would move the check to read time, where a failure is a
    broken enrichment run rather than a red form field.
    """
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        if not (row.discovered_schema or {}).get("tables"):
            raise SourceRejected("introspect the source before mapping it")
        validated = engine.validate_mapping(mapping, row.discovered_schema or {})
        row.mapping = validated
        row.status = "mapped"
        # A new mapping invalidates the old proof. This is the one that would actually hurt:
        # re-pointing a verified source at a different column while it stays selectable is the
        # wrong-attribution bug, shipped deliberately.
        row.dry_run = {}
        await _record(session, row, ok=True)
        return validated


async def run_dry_run(source_id: str) -> dict:
    """Rung 4 — read a bounded sample through the mapping. Writes nothing to the source.

    Promotes to ``verified`` only when the sample actually carried identities. A mapping that
    returns rows with no domain (or no linkedin/email) is pointed at the wrong column, and it is
    indistinguishable from success if you only count rows.
    """
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        if not row.mapping:
            raise SourceRejected("map the source before dry-running it")
        try:
            result = await engine.dry_run(row.dsn_encrypted, row.mapping)
        except Exception as exc:
            await _record(session, row, ok=False, error=str(exc))
            raise
        result["verified"] = bool(result.get("usable_rows"))
        row.dry_run = result
        if result["verified"]:
            row.status = USABLE_STATUS
            await _record(session, row, ok=True)
        else:
            # Not `failed` — the source is reachable and the query ran. The mapping is wrong, and
            # saying "failed" would send an operator to check the network instead of the columns.
            row.status = "mapped"
            await _record(
                session, row, ok=False, set_failed=False,
                error=(
                    f"dry run read {result.get('rows', 0)} row(s) but none carried an identity "
                    f"({result.get('identity_field')}); the mapping is on the wrong column"
                ),
            )
        return result


async def set_enabled(source_id: str, enabled: bool) -> SourceDatabase:
    """The operator's kill switch, separate from the ladder.

    Enabling is refused below ``verified``: that is what makes the dry run a gate rather than a
    suggestion. Disabling is always allowed — during an incident, "stop reading this" must never
    be blocked by a state machine.
    """
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        if enabled and row.status != USABLE_STATUS:
            raise SourceRejected(
                f"cannot enable a source in status {row.status!r}; a passing dry run is required"
            )
        row.enabled = bool(enabled)
        await session.commit()
        await session.refresh(row)
        return row


async def list_sources() -> list[SourceDatabase]:
    async with get_platform_sessionmaker()() as session:
        return list(
            (await session.scalars(select(SourceDatabase).order_by(SourceDatabase.name))).all()
        )


async def get_source(source_id: str) -> SourceDatabase:
    async with get_platform_sessionmaker()() as session:
        return await _load(session, source_id)


async def delete_source(source_id: str) -> None:
    async with get_platform_sessionmaker()() as session:
        row = await _load(session, source_id)
        await session.delete(row)
        await session.commit()


async def usable_sources() -> list[SourceDatabase]:
    """Sources the enrichment provider may read from — verified AND enabled.

    The single place that answers "can we use this?", so the provider, the console and the tests
    cannot drift apart on it.
    """
    async with get_platform_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(SourceDatabase).where(
                    SourceDatabase.status == USABLE_STATUS,
                    SourceDatabase.enabled.is_(True),
                )
            )
        ).all()
        return list(rows)
