"""Bring existing lists in — by CSV upload, or pulled from the connected CRM.

Distinct from `custom_fields.import_csv`, which *annotates* rows that already match and skips the
rest. These **create**.

Two rules the endpoints exist to enforce:

* **The mapping is explicit.** The operator says which CSV column is the company name and which is
  the website. Guessing from headers is how a "Company" column of parent-company names silently
  becomes the account name for every subsidiary.
* **The count is bounded.** Every imported account enters the refresh pipeline and starts spending
  credits, so an import that quietly pulls a 100,000-row CRM is a bill nobody agreed to.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from nexus.api.deps import Permission, Principal, get_tenant_session, require
from nexus.core.tenancy import TenantSession
from nexus.imports.crm_pull import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    clamp_limit,
    import_accounts_from_crm,
    import_contacts_from_crm,
)
from nexus.imports.csv_ingest import (
    ACCOUNT_INT_FIELDS,
    ACCOUNT_TEXT_FIELDS,
    CONTACT_TEXT_FIELDS,
    import_accounts_csv,
    import_contacts_csv,
)

router = APIRouter(prefix="/imports", tags=["imports"])

# 20 MB. A 50,000-row CSV of accounts is roughly 5 MB, so this is generous for the real case while
# still refusing an upload that would sit in memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# The fields a CSV column may be mapped onto, surfaced so the UI can build the mapping picker from
# the server rather than from a hard-coded list that drifts.
ACCOUNT_FIELDS = (*ACCOUNT_TEXT_FIELDS, "domain", *ACCOUNT_INT_FIELDS)
CONTACT_FIELDS = ("full_name", "email", *CONTACT_TEXT_FIELDS, "account_domain", "account_name")


class ImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    total_rows: int
    errors: list[str]


class ImportFieldsOut(BaseModel):
    account_fields: list[str]
    contact_fields: list[str]
    max_rows: int
    default_limit: int
    max_upload_bytes: int


def _parse_mapping(raw: str, allowed: tuple[str, ...]) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mapping: {exc}")
    if not isinstance(parsed, dict) or not parsed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "mapping must be a non-empty JSON object of {csv_column: field}")
    unknown = sorted({v for v in parsed.values() if v not in allowed})
    if unknown:
        # Named rather than ignored: a typo'd target silently drops that column, and the operator
        # discovers it only by noticing the data is missing later.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown target field(s): {', '.join(unknown)}. Allowed: {', '.join(allowed)}",
        )
    return {str(k): str(v) for k, v in parsed.items()}


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not content.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the file is empty")
    return content


@router.get("/fields", response_model=ImportFieldsOut)
async def importable_fields(
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportFieldsOut:
    """What a CSV column can be mapped onto. The mapping UI builds itself from this."""
    from nexus.imports.csv_ingest import MAX_ROWS

    return ImportFieldsOut(
        account_fields=list(ACCOUNT_FIELDS),
        contact_fields=list(CONTACT_FIELDS),
        max_rows=MAX_ROWS,
        default_limit=DEFAULT_LIMIT,
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )


@router.post("/accounts/csv", response_model=ImportResult)
async def upload_accounts_csv(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_accounts_csv(
        ts, content=await _read_upload(file), mapping=_parse_mapping(mapping, ACCOUNT_FIELDS)
    )
    await ts.commit()
    return ImportResult(**result)


@router.post("/contacts/csv", response_model=ImportResult)
async def upload_contacts_csv(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_contacts_csv(
        ts, content=await _read_upload(file), mapping=_parse_mapping(mapping, CONTACT_FIELDS)
    )
    await ts.commit()
    return ImportResult(**result)


async def _connector_or_400(ts: TenantSession):
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    connector = await resolve_crm_connector(ts)
    if connector is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No CRM is connected for this workspace. Connect one under Settings > Integrations.",
        )
    return connector


@router.post("/accounts/crm", response_model=ImportResult)
async def pull_accounts_from_crm(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_accounts_from_crm(ts, await _connector_or_400(ts), limit=clamp_limit(limit))
    await ts.commit()
    return ImportResult(**result)


@router.post("/contacts/crm", response_model=ImportResult)
async def pull_contacts_from_crm(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_contacts_from_crm(ts, await _connector_or_400(ts), limit=clamp_limit(limit))
    await ts.commit()
    return ImportResult(**result)
