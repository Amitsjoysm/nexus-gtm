"""Engagement reporting: a campaign's results, response times, and the Today plan (spec §11, §19).

Dark with the rest of the engine. A campaign's results are visible to whoever may open the campaign
(its owner, or a manager); response times are the caller's own unless a manager asks for the team;
Today is always the caller's own.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import _campaign, _is_manager, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class StepResultOut(BaseModel):
    step_index: int
    channel: str
    sent: int
    replies: int
    reply_rate: float


class CampaignReportOut(BaseModel):
    contacts: int
    sent: int
    bounced: int
    replied: int
    positive: int
    meetings: int
    reply_rate: float
    positive_rate: float
    steps: list[StepResultOut]
    categories: dict[str, int]


class ResponseTimeOut(BaseModel):
    user_id: str
    name: str
    answered: int
    waiting: int
    median_hours: float | None
    p90_hours: float | None


class TodayItemOut(BaseModel):
    kind: str
    title: str
    detail: str
    link: str
    at: datetime | None
    count: int


@router.get("/reports/campaigns/{campaign_id}", response_model=CampaignReportOut)
async def campaign_results(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignReportOut:
    from nexus.engagement.reports.service import campaign_report

    campaign = await _campaign(ts, campaign_id, principal)
    return CampaignReportOut(**(await campaign_report(ts, campaign)).as_dict())


@router.get("/reports/response-times", response_model=list[ResponseTimeOut])
async def response_times(
    team: bool = False, days: int = 30,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ResponseTimeOut]:
    from dataclasses import asdict

    from nexus.core.db import utcnow
    from nexus.engagement.reports.service import response_times as measure

    rows = await measure(ts, user_id=principal.user_id, team=team and _is_manager(principal),
                         now=utcnow(), days=max(1, min(days, 90)))
    return [ResponseTimeOut(**asdict(r)) for r in rows]


@router.get("/today", response_model=list[TodayItemOut])
async def today(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[TodayItemOut]:
    from nexus.core.db import utcnow
    from nexus.engagement.reports.today import today as plan

    return [TodayItemOut(**i.as_dict()) for i in await plan(ts, user_id=principal.user_id,
                                                            now=utcnow())]
