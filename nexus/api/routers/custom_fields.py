"""Custom-field definitions + CSV import of proprietary data.

GET/POST/DELETE ``/custom-fields`` manage the column catalog; POST ``/custom-fields/import``
upserts values onto existing accounts/contacts matched by domain/email. All tenant-scoped
and gated on ``manage_relevance`` (admin+) — proprietary data is an admin concern."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.custom_fields.service import CustomFieldError, CustomFieldService
from nexus.models.chat import CustomFieldDef

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])


class CustomFieldOut(BaseModel):
    id: str
    entity: str
    key: str
    label: str
    kind: str

    @classmethod
    def from_model(cls, d: CustomFieldDef) -> "CustomFieldOut":
        return cls(id=d.id, entity=d.entity, key=d.key, label=d.label, kind=d.kind)


class CreateFieldRequest(BaseModel):
    entity: str
    label: str
    key: str | None = None
    kind: str = "text"


class ImportResult(BaseModel):
    matched: int
    updated: int
    created_fields: list[str] = Field(default_factory=list)
    skipped: int


@router.get("", response_model=list[CustomFieldOut])
async def list_fields(
    entity: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> list[CustomFieldOut]:
    defs = await CustomFieldService().list_defs(ts, entity)
    return [CustomFieldOut.from_model(d) for d in defs]


@router.post("", response_model=CustomFieldOut, status_code=status.HTTP_201_CREATED)
async def create_field(
    body: CreateFieldRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> CustomFieldOut:
    try:
        d = await CustomFieldService().create_def(
            ts, entity=body.entity, key=body.key, label=body.label, kind=body.kind
        )
    except CustomFieldError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CustomFieldOut.from_model(d)


@router.delete("/{def_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_field(
    def_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> Response:
    if not await CustomFieldService().delete_def(ts, def_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/import", response_model=ImportResult)
async def import_csv(
    entity: str = Form(...),
    match_column: str = Form(...),
    mapping: str = Form(...),  # JSON object: {csv_column: field_key}
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ImportResult:
    try:
        mapping_obj = json.loads(mapping)
        if not isinstance(mapping_obj, dict):
            raise ValueError("mapping must be a JSON object")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mapping: {exc}")
    content = await file.read()
    try:
        result = await CustomFieldService().import_csv(
            ts, entity=entity, content=content, match_column=match_column, mapping=mapping_obj
        )
    except CustomFieldError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ImportResult(**result)
