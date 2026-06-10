"""Segment Campaign Engine endpoints: create a campaign over a List, review the draft
sample, approve once, and send. Create drives the draft phase inline to ``awaiting_approval``
for snappy feedback (the same inline pattern as orchestration run creation)."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.campaigns.schemas import (
    CampaignIn,
    CampaignOut,
    CampaignPreviewOut,
    CampaignTargetOut,
    CampaignDetailOut,
)
from nexus.campaigns.service import CampaignError, get_campaign_service
from nexus.core.config import get_settings
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_AWAITING_APPROVAL,
    CAMP_TERMINAL,
    TARGET_DRAFTED,
)
from nexus.models.workflow import ProspectList
from nexus.workers.tasks import tenant_session

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


async def _get_campaign(ts: TenantSession, campaign_id: str) -> Campaign:
    campaign = await ts.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    plist = await ts.get(ProspectList, body.list_id)
    if plist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "List not found")
    if body.cadence_id is not None:
        from nexus.models.cadence import Cadence

        if await ts.get(Cadence, body.cadence_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Cadence not found")
    svc = get_campaign_service()
    campaign = await svc.create(
        ts,
        name=body.name,
        list_id=body.list_id,
        icp=body.icp,
        sequence=body.sequence,
        created_by_user_id=principal.user_id,
        send_risky=body.send_risky,
        cadence_id=body.cadence_id,
        review_each_touch=body.review_each_touch,
    )
    # Drive the draft phase inline to the approval gate.
    await svc.run_draft_phase(ts, campaign)
    return CampaignOut.from_model(campaign)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CampaignOut]:
    stmt = ts.select(Campaign).order_by(Campaign.created_at.desc()).limit(100)
    rows = list((await ts.session.scalars(stmt)).all())
    return [CampaignOut.from_model(c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
async def get_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignDetailOut:
    campaign = await _get_campaign(ts, campaign_id)
    targets = await get_campaign_service().list_targets(ts, campaign.id)
    return CampaignDetailOut.from_models(campaign, targets)


@router.get("/{campaign_id}/preview", response_model=CampaignPreviewOut)
async def preview_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignPreviewOut:
    campaign = await _get_campaign(ts, campaign_id)
    targets = await get_campaign_service().list_targets(ts, campaign.id)
    sample_n = get_settings().campaign_preview_sample
    drafted = [t for t in targets if t.status == TARGET_DRAFTED][:sample_n]
    return CampaignPreviewOut(
        campaign_id=campaign.id,
        status=campaign.status,
        report=campaign.report or {},
        sample=[CampaignTargetOut.from_model(t) for t in drafted],
    )


@router.post("/{campaign_id}/approve", response_model=CampaignOut)
async def approve_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    campaign = await _get_campaign(ts, campaign_id)
    try:
        await get_campaign_service().approve_and_send(
            ts, campaign, decided_by=principal.user_id
        )
    except CampaignError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CampaignOut.from_model(campaign)


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    campaign = await _get_campaign(ts, campaign_id)
    await get_campaign_service().cancel(ts, campaign)
    return CampaignOut.from_model(campaign)


def _format_sse(seq: int, type_: str, data: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data)}\n\n"


async def _counts(ts: TenantSession, campaign_id: str) -> dict[str, int]:
    targets = await ts.list(CampaignTarget, CampaignTarget.campaign_id == campaign_id)
    counts: dict[str, int] = {}
    for t in targets:
        counts[t.status] = counts.get(t.status, 0) + 1
    return counts


async def _campaign_stream(
    tenant_id: str, campaign_id: str, request: Request
) -> AsyncIterator[str]:
    seq = 0
    last_snapshot: tuple | None = None
    for _ in range(600):  # ~5 min ceiling
        if await request.is_disconnected():
            return
        async with tenant_session(tenant_id) as ts:
            campaign = await ts.get(Campaign, campaign_id)
            if campaign is None:
                yield _format_sse(seq, "error", {"detail": "campaign not found"})
                return
            counts = await _counts(ts, campaign_id)
            campaign_status = campaign.status
            report = campaign.report or {}
        snapshot = (campaign_status, tuple(sorted(counts.items())))
        if snapshot != last_snapshot:
            seq += 1
            yield _format_sse(
                seq, "progress",
                {"status": campaign_status, "counts": counts, "report": report},
            )
            last_snapshot = snapshot
        if campaign_status in CAMP_TERMINAL or campaign_status == CAMP_AWAITING_APPROVAL:
            return
        await asyncio.sleep(0.5)


@router.get("/{campaign_id}/events")
async def stream_campaign_events(
    campaign_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    # EventSource can't set Authorization headers, so gate explicitly (mirrors runs SSE).
    if not has_permission(principal.role, Permission.manage_campaigns):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "lacks manage_campaigns")
    return StreamingResponse(
        _campaign_stream(principal.tenant_id, campaign_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
