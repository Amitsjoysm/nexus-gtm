"""Segment Campaign Engine endpoints: create a campaign over a List, review the draft
sample, approve once, and send. Create drives the draft phase inline to ``awaiting_approval``
for snappy feedback (the same inline pattern as orchestration run creation)."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.campaigns.schemas import (
    CampaignIn,
    CampaignOut,
    CampaignPreviewOut,
    CampaignTargetOut,
    CampaignDetailOut,
    LaunchFromSelectionIn,
)
from nexus.campaigns.service import CampaignError, get_campaign_service
from nexus.cadences.service import get_cadence_service
from nexus.core.config import get_settings
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.cadence import Cadence
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_AWAITING_APPROVAL,
    CAMP_TERMINAL,
    TARGET_DRAFTED,
)
from nexus.models.workflow import ListItem, ProspectList
from nexus.workers.tasks import tenant_session

# A sensible default multi-touch sequence for cadences auto-built from a discovery selection.
# Angles steer the per-touch compose (research_compose) so each email opens differently.
_DEFAULT_CADENCE_STEPS = [
    {"channel": "email", "delay_days": 0, "angle": "Personalized intro tied to a recent buying signal"},
    {"channel": "email", "delay_days": 3, "angle": "Value proposition mapped to their likely pains"},
    {"channel": "email", "delay_days": 6, "angle": "Short, polite break-up with a clear ask"},
]

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


@router.post(
    "/launch-from-selection",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
)
async def launch_from_selection(
    body: LaunchFromSelectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    """One-click 'Add to cadence' from the discovery results table.

    Builds an ad-hoc list from the selected accounts (and the accounts behind any selected
    contacts), attaches a cadence (a fresh 3-touch sequence, or an existing one), drafts a
    grounded personalized email per account, and parks the campaign at the approval gate. No
    email sends until a human approves — this is the 'draft + approval gate' autonomy level."""
    # Resolve the target accounts: explicit account ids + the accounts behind selected contacts.
    account_ids: set[str] = set(body.account_ids)
    for cid in body.contact_ids:
        contact = await ts.get(Contact, cid)
        if contact is not None:
            account_ids.add(contact.account_id)
    valid = [aid for aid in account_ids if await ts.get(Account, aid) is not None]
    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No valid accounts in the selection")

    # Resolve the cadence first so we fail before creating anything on a bad cadence_id.
    if body.mode == "existing_cadence":
        if body.cadence_id is None or await ts.get(Cadence, body.cadence_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Cadence not found")
        cadence_id = body.cadence_id
    else:
        cadence = await get_cadence_service().create_cadence(
            ts,
            name=f"{body.name} cadence",
            description="Auto-built from a discovery selection.",
            steps=_DEFAULT_CADENCE_STEPS,
            created_by_user_id=principal.user_id,
        )
        cadence_id = cadence.id

    # Build the ad-hoc list with explicit members (the list builder only does fit-floor segments).
    plist = ProspectList(
        tenant_id=ts.tenant_id,
        name=f"{body.name} — targets",
        owner_user_id=principal.user_id,
        filter={"source": "discovery_selection"},
    )
    ts.add(plist)
    await ts.flush()
    for aid in valid:
        ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=aid))
    await ts.flush()

    svc = get_campaign_service()
    campaign = await svc.create(
        ts,
        name=body.name,
        list_id=plist.id,
        icp=body.icp,
        sequence="ai-orchestrated-outbound",
        created_by_user_id=principal.user_id,
        cadence_id=cadence_id,
        review_each_touch=body.review_each_touch,
    )
    await svc.run_draft_phase(ts, campaign)  # holds at the approval gate; nothing sends yet
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
    # Reply attribution rollup: outcomes recorded against this campaign, by stage.
    from nexus.outcomes.service import get_outcome_service

    attribution = await get_outcome_service().campaign_attribution(ts, campaign.id)
    return CampaignDetailOut.from_models(campaign, targets, outcomes=attribution)


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
    # GROUP BY in SQL: the SSE progress loop calls this twice a second per open viewer, so
    # materializing every target row in Python (O(targets) each tick) would melt under a
    # large campaign. This returns one row per status instead.
    rows = (
        await ts.session.execute(
            select(CampaignTarget.status, func.count())
            .where(
                CampaignTarget.tenant_id == ts.tenant_id,
                CampaignTarget.campaign_id == campaign_id,
            )
            .group_by(CampaignTarget.status)
        )
    ).all()
    return {status: int(n) for status, n in rows}


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
