# nexus/api/routers/admin_sources.py
"""Staff-only registration of external source databases.

Gated on ``sources.manage``, which is deliberately not part of ``admins.manage``: registering a
data source and granting platform power are different acts (see ``billing/permissions.py``).

Two properties this router must never lose:

* **The DSN goes in and never comes back out.** No response model carries ``dsn_encrypted``, and
  the redacted form is what the console renders. A "show connection string" affordance would turn
  every read of this console into a credential disclosure.
* **No SQL arrives from the browser.** The mapping endpoint takes table/column *names*, which are
  checked against what introspection actually discovered and re-validated at query-build time. An
  admin UI that ran free SQL against a customer's production database is a blast radius and a
  compliance problem, and it adds nothing over a psql session.

Every mutation is audited with before/after, like every other platform-admin mutation.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action, snapshot
from nexus.billing.permissions import SOURCES_MANAGE
from nexus.core.db import get_platform_sessionmaker
from nexus.models.source_db import SourceDatabase
from nexus.sources import service
from nexus.sources.crypto import DsnUnsealable
from nexus.sources.engine import ENTITIES, SourceUnavailable
from nexus.sources.safety import SourceRejected

logger = logging.getLogger("nexus.api.admin_sources")

router = APIRouter(prefix="/admin/sources", tags=["admin-sources"])

# Fields audit snapshots compare. `dsn_encrypted` is deliberately absent — an audit row is read by
# more people than the console is, and a ciphertext in it is still a secret sitting somewhere new.
_AUDIT_FIELDS = ("name", "kind", "status", "enabled", "dsn_redacted", "last_error")


class SourceOut(BaseModel):
    """What the console sees. Note the absence of the connection string, in any form."""

    id: str
    name: str
    kind: str
    dsn_redacted: str
    status: str
    enabled: bool
    discovered_schema: dict = Field(default_factory=dict)
    mapping: dict = Field(default_factory=dict)
    dry_run: dict = Field(default_factory=dict)
    last_ok_at: str | None = None
    last_error: str = ""
    usable: bool = False


def _out(row: SourceDatabase) -> SourceOut:
    return SourceOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        dsn_redacted=row.dsn_redacted,
        status=row.status,
        enabled=row.enabled,
        discovered_schema=row.discovered_schema or {},
        mapping=row.mapping or {},
        dry_run=row.dry_run or {},
        last_ok_at=row.last_ok_at.isoformat() if row.last_ok_at else None,
        last_error=row.last_error or "",
        usable=row.is_usable(),
    )


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dsn: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="postgres", max_length=20)


class MappingIn(BaseModel):
    entity: str = Field(description=f"one of {sorted(ENTITIES)}")
    schema_name: str = Field(max_length=63)
    table: str = Field(max_length=63)
    # {app_field: source_column}. Names only — never an expression, never a fragment.
    columns: dict[str, str]


class EnabledIn(BaseModel):
    enabled: bool


def _fail(exc: Exception) -> HTTPException:
    """Map a domain error onto a status code.

    ``SourceRejected`` is 400: the admin asked for something we refuse (a private host, an unsafe
    identifier, a rung out of order) and the fix is in their hands. ``SourceUnavailable`` is 502:
    the *upstream* is the problem, and calling that a 400 would send an operator to re-check a
    form that was correct.
    """
    if isinstance(exc, SourceRejected):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "source database not found")
    if isinstance(exc, DsnUnsealable):
        return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    if isinstance(exc, SourceUnavailable):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    raise exc


async def _audit(principal: Principal, action: str, row: SourceDatabase | None,
                 before: dict | None = None, note: str = "") -> None:
    async with get_platform_sessionmaker()() as session:
        await record_admin_action(
            session,
            actor=principal.user_id,
            action=action,
            target=f"source_database:{row.id}" if row else "source_database",
            before=before or {},
            after=snapshot(row, _AUDIT_FIELDS),
            note=note,
        )
        await session.commit()


@router.get("", response_model=list[SourceOut])
async def list_sources(
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> list[SourceOut]:
    return [_out(r) for r in await service.list_sources()]


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(
    source_id: str,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    try:
        return _out(await service.get_source(source_id))
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def register_source(
    body: RegisterIn,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    """Register a DSN. Validated (SSRF guard) before it is stored, and sealed at rest."""
    try:
        row = await service.register(name=body.name, dsn=body.dsn, kind=body.kind)
    except Exception as exc:
        raise _fail(exc) from exc
    await _audit(principal, "source_database.register", row, before={})
    return _out(row)


@router.post("/{source_id}/test", response_model=dict)
async def test_source(
    source_id: str,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> dict:
    try:
        return await service.connect(source_id)
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/{source_id}/introspect", response_model=SourceOut)
async def introspect_source(
    source_id: str,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    try:
        before = snapshot(await service.get_source(source_id), _AUDIT_FIELDS)
        await service.introspect(source_id)
        row = await service.get_source(source_id)
    except Exception as exc:
        raise _fail(exc) from exc
    await _audit(principal, "source_database.introspect", row, before=before)
    return _out(row)


@router.put("/{source_id}/mapping", response_model=SourceOut)
async def put_mapping(
    source_id: str,
    body: MappingIn,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    """Map discovered columns onto app fields.

    ``schema_name`` rather than ``schema`` because ``schema`` is reserved on a pydantic model; the
    service and the stored mapping use ``schema``.
    """
    try:
        before = snapshot(await service.get_source(source_id), _AUDIT_FIELDS)
        await service.set_mapping(
            source_id,
            {
                "entity": body.entity,
                "schema": body.schema_name,
                "table": body.table,
                "columns": body.columns,
            },
        )
        row = await service.get_source(source_id)
    except Exception as exc:
        raise _fail(exc) from exc
    await _audit(principal, "source_database.map", row, before=before)
    return _out(row)


@router.post("/{source_id}/dry-run", response_model=SourceOut)
async def dry_run_source(
    source_id: str,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    """Read a bounded sample through the mapping and report it. Writes nothing, anywhere.

    Returns 200 with the result even when the dry run did not verify: "the mapping is on the wrong
    column" is a finding to read, not an error to retry, and the payload is the evidence.
    """
    try:
        before = snapshot(await service.get_source(source_id), _AUDIT_FIELDS)
        await service.run_dry_run(source_id)
        row = await service.get_source(source_id)
    except Exception as exc:
        raise _fail(exc) from exc
    await _audit(principal, "source_database.dry_run", row, before=before)
    return _out(row)


@router.put("/{source_id}/enabled", response_model=SourceOut)
async def set_enabled(
    source_id: str,
    body: EnabledIn,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> SourceOut:
    try:
        before = snapshot(await service.get_source(source_id), _AUDIT_FIELDS)
        row = await service.set_enabled(source_id, body.enabled)
    except Exception as exc:
        raise _fail(exc) from exc
    await _audit(principal, "source_database.enabled", row, before=before)
    return _out(row)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: str,
    principal: Principal = Depends(require_platform_permission(SOURCES_MANAGE)),
) -> Response:
    try:
        row = await service.get_source(source_id)
        before = snapshot(row, _AUDIT_FIELDS)
        await service.delete_source(source_id)
    except Exception as exc:
        raise _fail(exc) from exc
    async with get_platform_sessionmaker()() as session:
        await record_admin_action(
            session,
            actor=principal.user_id,
            action="source_database.delete",
            target=f"source_database:{source_id}",
            before=before,
            after={},
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
