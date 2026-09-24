"""Signal re-engagement (spec §19): people worth writing to again because something happened at
their company, a drafted email in the same thread, and the SDR's send.

Dark with the rest of the engine. Always the caller's own mailboxes: a suggestion is about a
conversation, and a conversation belongs to whoever is having it.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/restart", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class SuggestionOut(BaseModel):
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_id: str
    account_name: str
    campaign_id: str
    campaign_name: str
    reason: str
    signal_id: str
    signal_kind: str
    signal_title: str
    signal_at: datetime
    likelihood: str


class MessageIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str
    body: str


@router.get("", response_model=list[SuggestionOut])
async def list_suggestions(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[SuggestionOut]:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements.signal_reengage import suggestions

    rows = await suggestions(ts, user_id=principal.user_id, now=utcnow())
    return [SuggestionOut(**s.as_dict()) for s in rows]


@router.post("/{enrollment_id}/{signal_id}/draft")
async def draft(
    enrollment_id: str, signal_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage

    try:
        return await signal_reengage.draft(ts, user_id=principal.user_id,
                                           enrollment_id=enrollment_id, signal_id=signal_id,
                                           now=utcnow())
    except signal_reengage.RestartError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{enrollment_id}/{signal_id}/send")
async def send(
    enrollment_id: str, signal_id: str, body: MessageIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage

    try:
        result = await signal_reengage.send(
            ts, user_id=principal.user_id, enrollment_id=enrollment_id, signal_id=signal_id,
            subject=body.subject, body=body.body, now=utcnow())
    except signal_reengage.RestartError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"outcome": result.outcome, "reason": result.reason, "message_id": result.message_id}
