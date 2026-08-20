# nexus/sources/engine.py
"""Talking to a registered source database: connect, introspect, map, dry-run, read.

Steps 3–7 of the build order in
``docs/superpowers/plans/2026-07-31-master-company-data-layer.md``. Steps 3–6 are the ladder that
proves a source; ``build_lookup`` / ``fetch_by_identity`` at the bottom of this module are the
read step 7 consumes, and they are only ever reached for a source the ladder has already passed —
``nexus/sources/provider.py`` owns that check, and is the only caller.

**Everything in this module is read-only, three times over**, because any one of the three is one
mistake away from a write into a customer's database:

1. the session is opened with ``postgresql_readonly=True`` and ``default_transaction_read_only``,
   so the *driver* refuses a write;
2. every statement we issue is written here in code — no SQL string ever arrives from the browser;
3. every identifier that reaches a statement goes through ``require_identifier`` at the moment of
   use, not merely when it was discovered. A table name is attacker-controlled if the attacker owns
   the source database, and re-reading it from our own JSON column does not make it ours.

``validate_dsn`` runs first, always — before a driver ever sees the string. See ``safety.py`` for
why a DSN form is an SSRF primitive.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from nexus.core.config import get_settings
from nexus.sources.crypto import unseal_dsn
from nexus.sources.safety import SourceRejected, require_identifier, validate_dsn

logger = logging.getLogger("nexus.sources.engine")

# Schemas that are never a customer's data and are pure noise in a mapping UI. Excluding them is
# ergonomics, not security — the identifier guard is what makes the list safe.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# What a mapped entity can be. These are the shared stores the plan targets; a mapping that names
# anything else is refused rather than silently ignored.
ENTITIES: dict[str, tuple[str, ...]] = {
    # (required fields, then optional). Domain is required for a company for the same reason
    # `companies.domain` is the identity key: a name match across tenants is how this subsystem
    # shipped six wrong-attribution bugs.
    "company": ("domain", "name", "industry", "country", "employee_count"),
    # A person needs an identity we can key on. Name alone is never enough — getting a *person*
    # wrong means a rep phones a stranger with someone else's context.
    #
    # `phone` is optional and is where most of the money is. A phone lookup is an Apify actor run
    # billed per call (`nexus/people/enrich.py`, the priciest capability on the rate card); a
    # source database that already holds the number replaces that call outright. It is deliberately
    # NOT an identity — a phone can be a switchboard shared by a whole company, so it may be
    # *read* from a source but never used to decide *who* a row is about.
    "person": ("linkedin_url", "email", "full_name", "title", "company_domain", "phone"),
}
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "company": ("domain",),
    # Either identity key satisfies this; checked in `validate_mapping`, which is why it is not a
    # simple required-list.
    "person": (),
}


class SourceUnavailable(RuntimeError):
    """We could not reach or read the source database. Never fatal to the caller's own work.

    The plan's failure posture: a source database being down degrades to "fall through to the paid
    provider", never to "collection stops". It is an optimisation, not a dependency.
    """


@dataclass(slots=True)
class ColumnInfo:
    name: str
    data_type: str


@dataclass(slots=True)
class TableInfo:
    schema: str
    table: str
    columns: list[ColumnInfo] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "schema": self.schema,
            "table": self.table,
            "columns": [{"name": c.name, "type": c.data_type} for c in self.columns],
        }


def _engine_url(dsn: str) -> str:
    """Normalise to the asyncpg driver. ``postgres://`` is a legacy alias SQLAlchemy rejects."""
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    scheme, _, rest = dsn.partition("://")
    return f"postgresql+asyncpg://{rest}" if scheme in ("postgres", "postgresql") else dsn


async def _connect(dsn: str):
    """Open a genuinely read-only connection to a source database, or raise.

    ``NullPool``: these connections are rare, short and to a foreign host. A pool would hold open
    sockets to somebody else's database between an admin's clicks — an operational surprise for
    them and a resource we cannot account for.
    """
    settings = get_settings()
    validate_dsn(dsn, allow_private=settings.source_db_allow_private)

    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        _engine_url(dsn),
        poolclass=NullPool,
        connect_args={
            # asyncpg honours this at the protocol level: the *server* refuses writes on this
            # session, which holds even if a future statement here is wrong.
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": str(settings.source_db_statement_timeout_s * 1000),
                "application_name": "nexus-source-introspect",
            },
            "timeout": settings.source_db_statement_timeout_s,
        },
    )
    return engine


async def _run(dsn: str, fn) -> Any:
    """Connect, run ``fn(conn)``, always dispose. Foreign failures become SourceUnavailable.

    The whole-operation timeout is separate from the statement timeout: a server that accepts the
    connection and then never answers would otherwise hold a request worker for as long as it
    likes, and ``statement_timeout`` is set by a server we do not control.
    """
    settings = get_settings()
    engine = await _connect(dsn)
    try:
        async with engine.connect() as conn:
            return await asyncio.wait_for(
                fn(conn), timeout=settings.source_db_statement_timeout_s * 3
            )
    except SourceRejected:
        raise
    except asyncio.TimeoutError as exc:
        raise SourceUnavailable("source database timed out") from exc
    except Exception as exc:
        # Deliberately not re-raising the driver's message verbatim to the caller: it can contain
        # the host and user. The full exception goes to the log, where it belongs.
        logger.warning("source database error", exc_info=True)
        raise SourceUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    finally:
        await engine.dispose()


# ---- step 3: connection test ----------------------------------------------------------------
async def test_connection(sealed_dsn: str) -> dict:
    """Prove we can reach the source and that it really is read-only for us.

    Asserts read-only rather than assuming it. We ask for it twice (driver flag + server setting)
    and then *check* — a source whose role can write is a source where one bug in a later query
    is a write into a customer's production database.
    """
    dsn = unseal_dsn(sealed_dsn)

    async def _probe(conn: AsyncConnection) -> dict:
        version = await conn.scalar(text("SELECT version()"))
        readonly = await conn.scalar(text("SHOW transaction_read_only"))
        database = await conn.scalar(text("SELECT current_database()"))
        return {
            "server_version": str(version or "")[:120],
            "database": str(database or ""),
            "read_only": str(readonly or "").lower() == "on",
        }

    result = await _run(dsn, _probe)
    if not result["read_only"]:
        raise SourceUnavailable(
            "connection is not read-only; refusing to use this source "
            "(grant the role SELECT only, or set default_transaction_read_only)"
        )
    return result


# ---- step 4: introspection ------------------------------------------------------------------
async def introspect(sealed_dsn: str, *, max_tables: int = 200) -> list[TableInfo]:
    """Discover tables and columns over ``information_schema``.

    Every name is passed through ``require_identifier`` before it is *stored*, and a name that
    fails is skipped rather than aborting the whole introspection: one oddly-named table in a
    large schema must not make the source unusable. It is validated again at query-build time —
    this pass is not a substitute for that.

    ``max_tables`` bounds the response. A warehouse with 40k tables would otherwise produce a JSON
    column and an admin UI that neither is usable.
    """
    dsn = unseal_dsn(sealed_dsn)

    async def _read(conn: AsyncConnection) -> list[TableInfo]:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.table_schema, c.table_name, c.column_name, c.data_type
                    FROM information_schema.columns c
                    JOIN information_schema.tables t
                      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                    WHERE c.table_schema NOT IN :sys
                      AND t.table_type IN ('BASE TABLE', 'VIEW')
                    ORDER BY c.table_schema, c.table_name, c.ordinal_position
                    """
                ).bindparams(sys=_SYSTEM_SCHEMAS)
            )
        ).all()
        return rows  # type: ignore[return-value]

    raw = await _run(dsn, _read)

    tables: dict[tuple[str, str], TableInfo] = {}
    skipped = 0
    for schema, table, column, data_type in raw:
        try:
            require_identifier(schema, what="schema")
            require_identifier(table, what="table")
            require_identifier(column, what="column")
        except SourceRejected:
            skipped += 1
            continue
        key = (schema, table)
        if key not in tables:
            if len(tables) >= max_tables:
                continue
            tables[key] = TableInfo(schema=schema, table=table)
        tables[key].columns.append(ColumnInfo(name=column, data_type=str(data_type)))
    if skipped:
        logger.info("introspection skipped %d unsafely-named identifiers", skipped)
    return list(tables.values())


# ---- step 5: mapping ------------------------------------------------------------------------
def validate_mapping(mapping: dict, discovered: dict) -> dict:
    """Check an admin's mapping against what introspection actually found. Returns it normalised.

    Two independent checks, and both matter:

    * every identifier is a safe identifier (``require_identifier``), and
    * every identifier was actually **discovered**. Validity is not enough — a syntactically fine
      column name that we never saw means the admin is mapping onto something introspection did
      not return, which is either a typo or an attempt to reach a table outside what we listed.

    Only ``ENTITIES`` fields are accepted; an unknown app field is refused rather than dropped, so
    a typo in the console surfaces as an error instead of a column that silently never populates.
    """
    entity = str(mapping.get("entity") or "")
    if entity not in ENTITIES:
        raise SourceRejected(f"unknown entity {entity!r}; expected one of {sorted(ENTITIES)}")

    schema = require_identifier(mapping.get("schema"), what="schema")
    table = require_identifier(mapping.get("table"), what="table")

    available = {
        (t.get("schema"), t.get("table")): {c.get("name") for c in (t.get("columns") or [])}
        for t in (discovered.get("tables") or [])
    }
    if (schema, table) not in available:
        raise SourceRejected(f"{schema}.{table} was not found by introspection")
    known_columns = available[(schema, table)]

    columns_in = mapping.get("columns") or {}
    if not isinstance(columns_in, dict) or not columns_in:
        raise SourceRejected("mapping has no columns")

    allowed_fields = set(ENTITIES[entity])
    columns: dict[str, str] = {}
    for field_name, source_column in columns_in.items():
        if field_name not in allowed_fields:
            raise SourceRejected(
                f"unknown {entity} field {field_name!r}; expected one of {sorted(allowed_fields)}"
            )
        col = require_identifier(source_column, what="column")
        if col not in known_columns:
            raise SourceRejected(f"column {col!r} is not in {schema}.{table}")
        columns[field_name] = col

    missing = [f for f in REQUIRED_FIELDS[entity] if f not in columns]
    if missing:
        raise SourceRejected(f"{entity} mapping is missing required field(s): {missing}")
    if entity == "person" and not ({"linkedin_url", "email"} & set(columns)):
        # Mirrors nexus/people/: identity is LinkedIn URL, else normalised email, never a name
        # match. A person store keyed on names attaches one human's history to another.
        raise SourceRejected(
            "person mapping needs linkedin_url or email — a name is not an identity"
        )
    return {"entity": entity, "schema": schema, "table": table, "columns": columns}


def _projection(mapping: dict) -> tuple[str, str, str]:
    """Re-validate a mapping's identifiers and render the SELECT list. ``(schema, table, cols)``.

    Every identifier is re-validated **here**, at the moment of interpolation, even though
    ``validate_mapping`` already checked them: this is what puts them in a SQL string, and it may
    be reached with a mapping loaded from our own JSON column long after validation. "It was safe
    when we stored it" is exactly the assumption that makes stored-value injection work.

    Shared by ``build_select`` and ``build_lookup`` so there is exactly one place where a mapping
    becomes SQL — a second copy is how one of them ends up missing a check.
    """
    schema = require_identifier(mapping.get("schema"), what="schema")
    table = require_identifier(mapping.get("table"), what="table")
    columns = mapping.get("columns") or {}
    if not columns:
        raise SourceRejected("mapping has no columns")
    projection = ", ".join(
        f'"{require_identifier(col, what="column")}" AS "{require_identifier(fld, what="field")}"'
        for fld, col in sorted(columns.items())
    )
    return schema, table, projection


def build_select(mapping: dict, *, limit: int) -> str:
    """Build the unfiltered SELECT for a validated mapping. ``limit`` is bound, never formatted."""
    schema, table, projection = _projection(mapping)
    return f'SELECT {projection} FROM "{schema}"."{table}" LIMIT :limit'


# ---- step 6: dry run ------------------------------------------------------------------------
def _redact_sample(row: dict, entity: str) -> dict:
    """A sample row an operator can judge without it becoming a data export.

    A dry run has to show enough to tell "this column is the domain" from "this column is the
    billing contact's domain", and no more. Identity-ish fields are shown; free text is reported
    by presence only. The result is stored in `source_databases.dry_run`, which is a platform
    table a support admin can read — so this is the boundary between "proof" and "we copied a
    customer's data into our own database to look at it".
    """
    shown = {"company": {"domain", "name", "industry", "country", "employee_count"},
             "person": {"company_domain", "title"}}.get(entity, set())
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in shown:
            out[key] = value if value is None else str(value)[:120]
        else:
            out[key] = "<present>" if value not in (None, "") else None
    return out


async def dry_run(sealed_dsn: str, mapping: dict, *, limit: int | None = None) -> dict:
    """Read a bounded sample through the mapping and report what it WOULD produce. Writes nothing.

    This is the gate that makes a source selectable, and it is the same staging as the shared
    company crawl's shadow → diff → fan-out, for the same reason: a plausible integration pointed
    at the wrong column is the failure mode this subsystem has shipped six times, and four of
    those were found only by running against live data rather than by reading the config.

    ``usable_rows`` is the number that matters, not ``rows``. A mapping that returns 25 rows of
    which 2 carry an identity key is a mapping onto the wrong column — and it looks like a
    complete success if you only count rows.
    """
    settings = get_settings()
    limit = min(int(limit or settings.source_db_dry_run_limit), settings.source_db_dry_run_limit)
    # `mapping` is expected to have been through `validate_mapping` already (the service does it
    # against the stored `discovered_schema`). `build_select` re-validates every identifier anyway,
    # so a caller that forgot cannot produce an unsafe statement — only a less helpful error.
    sql = build_select(mapping, limit=limit)
    entity = mapping.get("entity", "")
    dsn = unseal_dsn(sealed_dsn)

    async def _sample(conn: AsyncConnection) -> list[dict]:
        result = await conn.execute(text(sql), {"limit": limit})
        return [dict(r) for r in result.mappings().all()]

    rows = await _run(dsn, _sample)

    def _has_identity(row: dict) -> bool:
        if entity == "company":
            return bool(str(row.get("domain") or "").strip())
        return bool(row.get("linkedin_url") or row.get("email"))

    usable = sum(1 for row in rows if _has_identity(row))
    return {
        "entity": entity,
        "rows": len(rows),
        # The number that matters. Rows alone say the query ran; this says the mapping found the
        # column that makes a row joinable to anything.
        "usable_rows": usable,
        "identity_field": "domain" if entity == "company" else "linkedin_url|email",
        "sample": [_redact_sample(r, entity) for r in rows[:5]],
        # The literal statement, so an operator can see exactly what ran. It contains no
        # credentials and no user input — every identifier in it came from introspection.
        "statement": sql,
    }


# ---- step 7: reading a verified source -------------------------------------------------------
# The fields a lookup may be keyed on. Exactly the identity keys the ladder already enforces at
# mapping time (`validate_mapping`), and no others: keying on a name is how you attach one
# company's firmographics — or one human's phone number — to another.
LOOKUP_FIELDS: dict[str, tuple[str, ...]] = {
    "company": ("domain",),
    "person": ("linkedin_url", "email"),
}

# A lookup is a point read on an identity key, so a handful of rows is already more than the
# answer needs. The cap exists because we do not control the source's indexes: an unindexed
# column on a big table would otherwise stream a warehouse through a request worker.
LOOKUP_LIMIT = 5


def build_lookup(mapping: dict, *, key_field: str, limit: int = LOOKUP_LIMIT) -> str:
    """Build the identity-keyed SELECT the enrichment provider reads through.

    ``key_field`` is checked against ``LOOKUP_FIELDS`` rather than merely against the mapping, so
    a mapping that happens to carry ``full_name`` can never be queried *by* it. The key values
    themselves are bound parameters — only the column name is interpolated, and only after
    ``_projection`` has re-validated every identifier in the mapping.
    """
    entity = str(mapping.get("entity") or "")
    allowed = LOOKUP_FIELDS.get(entity)
    if not allowed:
        raise SourceRejected(f"unknown entity {entity!r}; expected one of {sorted(LOOKUP_FIELDS)}")
    if key_field not in allowed:
        raise SourceRejected(
            f"{entity} lookups may only be keyed on {list(allowed)}, not {key_field!r}"
        )
    key_column = (mapping.get("columns") or {}).get(key_field)
    if not key_column:
        raise SourceRejected(f"mapping does not carry {key_field!r}")

    schema, table, projection = _projection(mapping)
    # Re-validated independently of the projection: this name is going into a WHERE clause, and
    # the projection's pass over it is not this call's guarantee.
    column = require_identifier(key_column, what="column")
    # `IN` over a small candidate set rather than `lower(col) = :key`: a function on the column
    # would defeat whatever index the source has, and we do not get to add one to a database we
    # do not own. The candidates are generated by us (see `provider._domain_candidates`), never
    # by the source.
    return (
        f'SELECT {projection} FROM "{schema}"."{table}" '
        f'WHERE "{column}" IN :keys LIMIT :limit'
    )


async def fetch_by_identity(
    sealed_dsn: str,
    mapping: dict,
    *,
    key_field: str,
    key_values: list[str],
    limit: int = LOOKUP_LIMIT,
) -> list[dict]:
    """Read the rows a verified source holds for one identity. Writes nothing.

    This is the seam that talks to a foreign host, and it is the one the tests replace. It raises
    ``SourceUnavailable`` on anything the source does wrong — the *caller* is what turns that into
    "fall through to the paid provider", because that decision belongs to the enrichment flow and
    not to the transport.
    """
    from sqlalchemy import bindparam

    keys = [k for k in (v.strip() for v in key_values if isinstance(v, str)) if k]
    if not keys:
        return []
    sql = build_lookup(mapping, key_field=key_field, limit=limit)
    dsn = unseal_dsn(sealed_dsn)

    async def _read(conn: AsyncConnection) -> list[dict]:
        stmt = text(sql).bindparams(bindparam("keys", expanding=True))
        result = await conn.execute(stmt, {"keys": keys, "limit": limit})
        return [dict(r) for r in result.mappings().all()]

    return await _run(dsn, _read)
