"""The reply desk API: the queue, the suggested answer, and the SDR's decision (spec §9).

Dark with the rest of the engine until `engagement_campaigns_enabled` is on. A rep works their own
mailboxes; `?team=true` is honoured only for a manager, who can also reassign an item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import require_campaigns_enabled
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/desk", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class QueueItemOut(BaseModel):
    id: str
    message_id: str
    category: str
    corrected_category: str | None
    confidence: float
    resolved_date: date | None
    status: str
    decision: str | None
    assigned_user_id: str | None
    contact_id: str | None
    account_id: str | None
    contact_name: str
    contact_email: str
    account_name: str
    subject: str
    preview: str
    received_at: datetime | None
    responded_at: datetime | None


class ScheduledOut(BaseModel):
    enrollment_id: str
    contact_id: str
    contact_name: str
    account_name: str
    campaign_id: str
    status: str
    status_reason: str | None
    due_at: datetime | None


class ConversationMessageOut(BaseModel):
    id: str
    direction: str
    subject: str
    body: str
    at: datetime | None


class ColleagueOut(BaseModel):
    enrollment_id: str
    contact_id: str
    campaign_id: str


class ItemOut(QueueItemOut):
    body: str
    suggested_response: str | None
    conversation: list[ConversationMessageOut]
    paused_colleagues: list[ColleagueOut]


class ResponseIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str
    body: str


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str
    reengage_on: date | None = None
    note: str = ""


class CorrectionIn(BaseModel):
    model_config = {"extra": "forbid"}

    category: str


def _is_manager(principal: Principal) -> bool:
    return has_permission(Role(principal.role), Permission.manage_engagement)


@dataclass(slots=True)
class _Names:
    contacts: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)

    def contact(self, contact_id: str | None):
        return self.contacts.get(contact_id)

    def account(self, account_id: str | None):
        return self.accounts.get(account_id)


async def _names(ts: TenantSession, contact_ids, account_ids) -> _Names:
    """Who each row is with, in two queries for the whole page rather than two per row."""
    from nexus.models.account import Account, Contact

    contact_ids = {c for c in contact_ids if c}
    account_ids = {a for a in account_ids if a}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids |= {c.account_id for c in contacts.values() if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    return _Names(contacts=contacts, accounts=accounts)


def _row(classification, message, names: _Names) -> dict:
    body = (getattr(message, "body_text", "") or "")
    contact = names.contact(classification.contact_id)
    account = names.account(classification.account_id
                            or getattr(contact, "account_id", None))
    return {
        "id": classification.id, "message_id": classification.message_id,
        "category": classification.category,
        "corrected_category": classification.corrected_category,
        "confidence": classification.confidence, "resolved_date": classification.resolved_date,
        "status": classification.status, "decision": classification.decision,
        "assigned_user_id": classification.assigned_user_id,
        "contact_id": classification.contact_id, "account_id": classification.account_id,
        # A reply matched to nobody still shows who wrote it, from the message itself.
        "contact_name": getattr(contact, "full_name", "") or "",
        "contact_email": getattr(contact, "email", "") or getattr(message, "from_addr", "") or "",
        "account_name": getattr(account, "name", "") or "",
        "subject": getattr(message, "subject", "") or "",
        "preview": " ".join(body.split())[:280],
        "received_at": getattr(message, "received_at", None),
        "responded_at": classification.responded_at,
    }


async def _classification(ts: TenantSession, classification_id: str, principal: Principal):
    from nexus.models.engagement import MailboxConnection, ReplyClassification

    row = await ts.get(ReplyClassification, classification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reply not found")
    mailbox = await ts.get(MailboxConnection, row.mailbox_connection_id)
    if mailbox is None or (mailbox.owner_user_id != principal.user_id
                           and not _is_manager(principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reply not found")
    return row


def _refuse(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("", response_model=list[QueueItemOut])
async def queue(
    tab: str = "needs_action", team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[QueueItemOut]:
    from nexus.engagement.desk import service
    from nexus.models.engagement import EngagementMessage

    scope = team and _is_manager(principal)
    if tab not in ("needs_action", "handled"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tab")
    reader = service.needs_action if tab == "needs_action" else service.handled
    rows = await reader(ts, user_id=principal.user_id, team=scope)
    if not rows:
        return []
    messages = {m.id: m for m in await ts.list(
        EngagementMessage, EngagementMessage.id.in_([r.message_id for r in rows]))}
    names = await _names(ts, [r.contact_id for r in rows], [r.account_id for r in rows])
    return [QueueItemOut(**_row(r, messages.get(r.message_id), names)) for r in rows]


@router.get("/scheduled", response_model=list[ScheduledOut])
async def scheduled(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ScheduledOut]:
    from nexus.engagement.desk import service

    rows = await service.scheduled(ts, user_id=principal.user_id,
                                   team=team and _is_manager(principal))
    names = await _names(ts, [e.contact_id for e in rows], [e.account_id for e in rows])
    return [ScheduledOut(
        enrollment_id=e.id, contact_id=e.contact_id,
        contact_name=getattr(names.contact(e.contact_id), "full_name", "") or "",
        account_name=getattr(names.account(e.account_id), "name", "") or "",
        campaign_id=e.campaign_id, status=e.status, status_reason=e.status_reason,
        due_at=e.snoozed_until) for e in rows]


@router.get("/{classification_id}", response_model=ItemOut)
async def read_item(
    classification_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> ItemOut:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    detail = await service.item(ts, classification)
    names = _Names(
        contacts={detail.contact.id: detail.contact} if detail.contact else {},
        accounts={detail.account.id: detail.account} if detail.account else {})
    return ItemOut(
        **_row(classification, detail.message, names),
        body=getattr(detail.message, "body_text", "") or "",
        suggested_response=classification.suggested_response,
        conversation=[ConversationMessageOut(
            id=m.id, direction=m.direction, subject=m.subject or "", body=m.body_text or "",
            at=m.sent_at or m.received_at or m.created_at) for m in detail.conversation],
        paused_colleagues=[ColleagueOut(enrollment_id=e.id, contact_id=e.contact_id,
                                        campaign_id=e.campaign_id)
                           for e in detail.paused_colleagues])


@router.post("/{classification_id}/draft")
async def draft(
    classification_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        return await service.draft_response(ts, classification, user_id=principal.user_id)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/send")
async def send(
    classification_id: str, body: ResponseIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        result = await service.send_response(ts, classification, user_id=principal.user_id,
                                             subject=body.subject, body=body.body)
    except service.DeskError as exc:
        raise _refuse(exc) from exc
    return {"outcome": result.outcome, "reason": result.reason, "message_id": result.message_id}


@router.post("/{classification_id}/save-draft")
async def save_draft(
    classification_id: str, body: ResponseIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        draft_id = await service.save_to_drafts(ts, classification, subject=body.subject,
                                                body=body.body)
    except service.DeskError as exc:
        raise _refuse(exc) from exc
    return {"provider_draft_id": draft_id}


@router.post("/{classification_id}/decide", status_code=204, response_model=None)
async def decide(
    classification_id: str, body: DecisionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        await service.decide(ts, classification, body.decision, user_id=principal.user_id,
                             reengage_on=body.reengage_on, note=body.note)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/correct", status_code=204, response_model=None)
async def correct(
    classification_id: str, body: CorrectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    try:
        await service.correct(ts, classification, body.category, user_id=principal.user_id)
    except service.DeskError as exc:
        raise _refuse(exc) from exc


@router.post("/{classification_id}/assign", status_code=204, response_model=None)
async def assign(
    classification_id: str, user_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> None:
    from nexus.engagement.desk import service

    classification = await _classification(ts, classification_id, principal)
    await service.reassign(ts, classification, user_id=user_id)


@router.post("/colleagues/{enrollment_id}/{action}", status_code=204, response_model=None)
async def colleague(
    enrollment_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.desk import service
    from nexus.models.engagement import EngagementEnrollment

    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    try:
        if action == "resume":
            await service.resume_colleague(ts, enrollment, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_colleague(ts, enrollment, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except (service.DeskError, ValueError) as exc:
        raise _refuse(exc) from exc
