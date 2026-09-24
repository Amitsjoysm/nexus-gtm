"""Insights for the engagement screens (spec §18.5, D26): what may be said about each person, how
likely they are to reply to you, and when to send.

Dark with the rest of the engine. Every rule about what may be shown lives in
`nexus/engagement/insights/rules.py`; this router only batches and serialises.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import _campaign, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/insights", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


@router.get("/contacts")
async def contact_insights(
    ids: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[dict]:
    """``ids`` is a comma-separated list, at most 100: one call per screen, not one per row."""
    from nexus.core.db import utcnow
    from nexus.engagement.insights.service import MAX_CONTACTS, for_contacts

    wanted = [i for i in (s.strip() for s in ids.split(",")) if i]
    if len(wanted) > MAX_CONTACTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Ask about at most {MAX_CONTACTS} contacts at a time")
    return [i.as_dict() for i in await for_contacts(ts, wanted, now=utcnow())]


@router.get("/campaigns/{campaign_id}/best-time")
async def campaign_best_time(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.insights.service import campaign_best_time as suggest

    campaign = await _campaign(ts, campaign_id, principal)
    return (await suggest(ts, campaign, now=utcnow())).as_dict()
