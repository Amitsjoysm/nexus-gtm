"""Cadence endpoints: define multi-touch cadences, inspect a campaign's enrollments and
touches, and control individual enrollments (pause/resume/stop, approve/reject a touch).

The router carries no prefix — cadence routes live under ``/cadences`` while enrollment and
report routes hang off ``/campaigns/{id}`` and ``/enrollments/{id}`` — so paths are written
in full. Every endpoint is gated by ``manage_campaigns`` (the same permission as campaigns)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.cadences.schemas import (
    CadenceEnrollmentOut,
    CadenceIn,
    CadenceOut,
    CadenceReportOut,
    EnrollmentDetailOut,
)
from nexus.cadences.service import CadenceError, get_cadence_service
from nexus.core.db import utcnow
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.cadence import Cadence, CadenceEnrollment, CadenceTouch

router = APIRouter(tags=["cadences"])


class _CadencePatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class _ApproveTouchIn(BaseModel):
    edited_body: str | None = None


class _RejectTouchIn(BaseModel):
    stop: bool = False


async def _get_cadence(ts: TenantSession, cadence_id: str) -> Cadence:
    c = await ts.get(Cadence, cadence_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cadence not found")
    return c


async def _get_enrollment(ts: TenantSession, enrollment_id: str) -> CadenceEnrollment:
    e = await ts.get(CadenceEnrollment, enrollment_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    return e


# ----- Cadence definitions ----------------------------------------------------------
@router.post("/cadences", response_model=CadenceOut, status_code=status.HTTP_201_CREATED)
async def create_cadence(
    body: CadenceIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    svc = get_cadence_service()
    try:
        cadence = await svc.create_cadence(
            ts,
            name=body.name,
            description=body.description,
            steps=[s.model_dump() for s in body.steps],
            created_by_user_id=principal.user_id,
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    steps = await svc.list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.get("/cadences", response_model=list[CadenceOut])
async def list_cadences(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CadenceOut]:
    svc = get_cadence_service()
    stmt = ts.select(Cadence).order_by(Cadence.created_at.desc()).limit(100)
    cadences = list((await ts.session.scalars(stmt)).all())
    out: list[CadenceOut] = []
    for c in cadences:
        out.append(CadenceOut.from_models(c, await svc.list_steps(ts, c.id)))
    return out


@router.get("/cadences/{cadence_id}", response_model=CadenceOut)
async def get_cadence(
    cadence_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    cadence = await _get_cadence(ts, cadence_id)
    steps = await get_cadence_service().list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.patch("/cadences/{cadence_id}", response_model=CadenceOut)
async def update_cadence(
    cadence_id: str,
    body: _CadencePatchIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    cadence = await _get_cadence(ts, cadence_id)
    if body.name is not None:
        cadence.name = body.name
    if body.description is not None:
        cadence.description = body.description
    if body.is_active is not None:
        cadence.is_active = body.is_active
    await ts.flush()
    steps = await get_cadence_service().list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.delete("/cadences/{cadence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_cadence(
    cadence_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> Response:
    # Soft delete: existing enrollments may still reference this cadence, so deactivate
    # rather than orphan them. Idempotent.
    cadence = await _get_cadence(ts, cadence_id)
    cadence.is_active = False
    await ts.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----- Enrollments + report ---------------------------------------------------------
@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[CadenceEnrollmentOut])
async def list_enrollments(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CadenceEnrollmentOut]:
    rows = await ts.list(CadenceEnrollment, CadenceEnrollment.campaign_id == campaign_id)
    return [CadenceEnrollmentOut.from_model(e) for e in rows]


@router.get("/campaigns/{campaign_id}/cadence-report", response_model=CadenceReportOut)
async def campaign_cadence_report(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceReportOut:
    report = await get_cadence_service().cadence_report(ts, campaign_id)
    return CadenceReportOut(**report)


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentDetailOut)
async def get_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> EnrollmentDetailOut:
    e = await _get_enrollment(ts, enrollment_id)
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    touches.sort(key=lambda t: t.step_index)
    return EnrollmentDetailOut.from_models(e, touches)


@router.post("/enrollments/{enrollment_id}/pause", response_model=CadenceEnrollmentOut)
async def pause_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().pause(ts, e)
    return CadenceEnrollmentOut.from_model(e)


@router.post("/enrollments/{enrollment_id}/resume", response_model=CadenceEnrollmentOut)
async def resume_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().resume(ts, e, now=utcnow())
    return CadenceEnrollmentOut.from_model(e)


@router.post("/enrollments/{enrollment_id}/stop", response_model=CadenceEnrollmentOut)
async def stop_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().stop(ts, e)
    return CadenceEnrollmentOut.from_model(e)


@router.post(
    "/enrollments/{enrollment_id}/touches/{step_index}/approve",
    response_model=CadenceEnrollmentOut,
)
async def approve_touch(
    enrollment_id: str,
    step_index: int,
    body: _ApproveTouchIn | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    edited_body = body.edited_body if body else None
    try:
        await get_cadence_service().approve_touch(
            ts, e, step_index, now=utcnow(), edited_body=edited_body
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CadenceEnrollmentOut.from_model(e)


@router.post(
    "/enrollments/{enrollment_id}/touches/{step_index}/reject",
    response_model=CadenceEnrollmentOut,
)
async def reject_touch(
    enrollment_id: str,
    step_index: int,
    body: _RejectTouchIn | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    stop = body.stop if body else False
    try:
        await get_cadence_service().reject_touch(
            ts, e, step_index, now=utcnow(), stop=stop
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CadenceEnrollmentOut.from_model(e)
