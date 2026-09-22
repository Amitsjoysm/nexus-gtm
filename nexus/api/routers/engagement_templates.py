"""Sequence templates: reusable step lists a campaign starts from (spec §9 — Cadences becomes this).

A template is only a list of step specs; a campaign copies them at creation, so editing a template
later never changes a campaign that is already sending. Archiving hides a template from the picker
and deletes nothing, because a campaign records which template it came from.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import StepIn, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/templates", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class TemplateIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    steps: list[StepIn] = Field(min_length=1)


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    steps: list[dict]
    created_at: datetime


def _out(row) -> TemplateOut:
    return TemplateOut(id=row.id, name=row.name, description=row.description or "",
                       steps=list(row.steps or []), created_at=row.created_at)


def _validated(body: TemplateIn) -> list[dict]:
    from nexus.engagement.sequences.service import CampaignError, validate_steps

    try:
        return validate_steps([s.model_dump() for s in body.steps])
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[TemplateOut]:
    from nexus.models.engagement import SequenceTemplate

    rows = await ts.list(SequenceTemplate, SequenceTemplate.archived_at.is_(None))
    return [_out(r) for r in sorted(rows, key=lambda r: r.name.lower())]


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> TemplateOut:
    from nexus.models.engagement import SequenceTemplate

    row = SequenceTemplate(name=body.name.strip(), description=body.description.strip(),
                           steps=_validated(body), created_by_user_id=principal.user_id)
    ts.add(row)
    await ts.flush()
    return _out(row)


@router.put("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: str, body: TemplateIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_engagement)),
) -> TemplateOut:
    from nexus.models.engagement import SequenceTemplate

    row = await ts.get(SequenceTemplate, template_id)
    if row is None or row.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    row.name, row.description, row.steps = body.name.strip(), body.description.strip(), \
        _validated(body)
    await ts.flush()
    return _out(row)


@router.delete("/{template_id}", status_code=204, response_model=None)
async def archive_template(
    template_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_engagement)),
) -> None:
    from nexus.core.db import utcnow
    from nexus.models.engagement import SequenceTemplate

    row = await ts.get(SequenceTemplate, template_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    row.archived_at = row.archived_at or utcnow()
    await ts.flush()
