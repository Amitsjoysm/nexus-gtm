"""Engagement campaigns: build, draft, review, launch, and steer (spec §9).

Dark until the cutover: every route answers 404 while `engagement_campaigns_enabled` is off, so the
new engine cannot be reached by URL before it is switched on — the same effect as the hidden nav
item, enforced where it matters.

An SDR works their own campaigns (`run_engagement`); a manager can act on anyone's
(`manage_engagement`). A campaign sends from its owner's own mailbox, so ownership is not a label:
it decides whose name is on every email.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession


async def require_campaigns_enabled() -> None:
    from nexus.engagement import config

    if not config.campaigns_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


router = APIRouter(prefix="/engagement", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class StepIn(BaseModel):
    model_config = {"extra": "forbid"}

    channel: str = "email"
    angle: str = ""
    timing_mode: str = "auto"
    delay_business_days: int = 0
    send_time_local: str | None = None
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])


class CampaignIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    mailbox_id: str
    steps: list[StepIn] = Field(default_factory=list)
    template_id: str | None = None
    review_every_touch: bool = False
    first_send_mode: str = "on_approval"
    first_send_at: datetime | None = None
    timezone_mode: str = "contact"
    source_list_id: str | None = None


class StepOut(StepIn):
    step_index: int


class CampaignOut(BaseModel):
    id: str
    name: str
    owner_user_id: str
    mailbox_connection_id: str | None
    status: str
    pause_reason: str | None
    review_every_touch: bool
    first_send_mode: str
    first_send_at: datetime | None
    timezone_mode: str
    launched_at: datetime | None
    steps: list[StepOut]
    counts: dict[str, int]


class ContactsIn(BaseModel):
    model_config = {"extra": "forbid"}

    contact_ids: list[str] = Field(min_length=1, max_length=2000)


class ReviewItemOut(BaseModel):
    enrollment_id: str
    contact_id: str
    contact_name: str = ""
    contact_email: str = ""
    contact_title: str = ""
    account_name: str = ""
    message_id: str | None
    subject: str
    body: str
    quality_problems: list[str]
    status: str


class ApproveIn(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str | None = None
    body: str | None = None


class MoveIn(BaseModel):
    model_config = {"extra": "forbid"}

    when: datetime


class EnrollmentOut(BaseModel):
    id: str
    contact_id: str
    account_id: str
    contact_name: str = ""
    contact_email: str = ""
    contact_title: str = ""
    account_name: str = ""
    status: str
    status_reason: str | None
    current_step_index: int
    next_action_at: datetime | None
    snoozed_until: datetime | None
    contact_timezone: str


def _is_manager(principal: Principal) -> bool:
    return has_permission(Role(principal.role), Permission.manage_engagement)


async def _campaign(ts: TenantSession, campaign_id: str, principal: Principal):
    from nexus.models.engagement import EngagementCampaign

    campaign = await ts.get(EngagementCampaign, campaign_id)
    if campaign is None or (campaign.owner_user_id != principal.user_id
                            and not _is_manager(principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


async def _out(ts: TenantSession, campaign) -> CampaignOut:
    from nexus.engagement.sequences.service import steps_of
    from nexus.models.engagement import EngagementEnrollment

    counts: dict[str, int] = {}
    for enrollment in await ts.list(EngagementEnrollment,
                                    EngagementEnrollment.campaign_id == campaign.id):
        counts[enrollment.status] = counts.get(enrollment.status, 0) + 1
    return CampaignOut(
        id=campaign.id, name=campaign.name, owner_user_id=campaign.owner_user_id,
        mailbox_connection_id=campaign.mailbox_connection_id, status=campaign.status,
        pause_reason=campaign.pause_reason, review_every_touch=campaign.review_every_touch,
        first_send_mode=campaign.first_send_mode, first_send_at=campaign.first_send_at,
        timezone_mode=campaign.timezone_mode, launched_at=campaign.launched_at,
        steps=[StepOut(step_index=s.step_index, channel=s.channel, angle=s.angle,
                       timing_mode=s.timing_mode, delay_business_days=s.delay_business_days,
                       send_time_local=s.send_time_local,
                       allowed_weekdays=list(s.allowed_weekdays or []))
               for s in await steps_of(ts, campaign)],
        counts=counts)


def _refuse(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


async def _people(ts: TenantSession, contact_ids, account_ids) -> tuple[dict, dict]:
    """Contacts and accounts by id, in two queries for the whole page rather than two per row."""
    from nexus.models.account import Account, Contact

    contact_ids = {c for c in contact_ids if c}
    account_ids = {a for a in account_ids if a}
    contacts = {c.id: c for c in await ts.list(Contact, Contact.id.in_(contact_ids))} \
        if contact_ids else {}
    account_ids |= {c.account_id for c in contacts.values() if c.account_id}
    accounts = {a.id: a for a in await ts.list(Account, Account.id.in_(account_ids))} \
        if account_ids else {}
    return contacts, accounts


def _named(contact, account) -> dict:
    return {"contact_name": getattr(contact, "full_name", "") or "",
            "contact_email": getattr(contact, "email", "") or "",
            "contact_title": getattr(contact, "title", "") or "",
            "account_name": getattr(account, "name", "") or ""}


class CandidateOut(BaseModel):
    contact_id: str
    full_name: str
    title: str
    seniority: str
    email: str
    email_status: str
    account_id: str
    account_name: str
    blocked: bool


class TimelineEntryOut(BaseModel):
    message_id: str
    direction: str
    kind: str
    status: str
    subject: str
    preview: str
    at: datetime | None
    contact_id: str | None
    contact_name: str
    campaign_id: str | None
    campaign_name: str
    category: str | None


@router.get("/candidates", response_model=list[CandidateOut])
async def list_candidates(
    list_id: str | None = None, q: str | None = None, title: str | None = None,
    seniority: str | None = None, limit: int = 500,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[CandidateOut]:
    """People who could be added to a campaign: from a saved list, by title and seniority."""
    from dataclasses import asdict

    from nexus.engagement.sequences.candidates import candidates

    rows = await candidates(ts, list_id=list_id, q=q, title=title, seniority=seniority,
                            limit=limit)
    return [CandidateOut(**asdict(r)) for r in rows]


@router.get("/timeline", response_model=list[TimelineEntryOut])
async def get_timeline(
    contact_id: str | None = None, account_id: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[TimelineEntryOut]:
    """Everything sent to and received from a contact, or everyone at an account."""
    from dataclasses import asdict

    from nexus.engagement.sequences.candidates import timeline

    if not contact_id and not account_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Name a contact_id or an account_id")
    rows = await timeline(ts, contact_id=contact_id, account_id=account_id)
    return [TimelineEntryOut(**asdict(r)) for r in rows]


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[CampaignOut]:
    from nexus.models.engagement import EngagementCampaign

    where = [] if (team and _is_manager(principal)) else [
        EngagementCampaign.owner_user_id == principal.user_id]
    rows = await ts.list(EngagementCampaign, *where)
    return [await _out(ts, c) for c in sorted(rows, key=lambda c: c.created_at, reverse=True)]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create(
    body: CampaignIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    from nexus.engagement.sequences.service import CampaignError, create_campaign

    try:
        campaign = await create_campaign(
            ts, name=body.name, owner_user_id=principal.user_id, mailbox_id=body.mailbox_id,
            steps=[s.model_dump() for s in body.steps], template_id=body.template_id,
            review_every_touch=body.review_every_touch, first_send_mode=body.first_send_mode,
            first_send_at=body.first_send_at, timezone_mode=body.timezone_mode,
            source_list_id=body.source_list_id)
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, campaign)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    return await _out(ts, await _campaign(ts, campaign_id, principal))


@router.put("/campaigns/{campaign_id}/steps", response_model=CampaignOut)
async def put_steps(
    campaign_id: str, body: list[StepIn],
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> CampaignOut:
    from nexus.engagement.sequences.service import CampaignError, replace_steps

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        await replace_steps(ts, campaign, [s.model_dump() for s in body])
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, campaign)


@router.post("/campaigns/{campaign_id}/contacts")
async def add_contacts(
    campaign_id: str, body: ContactsIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import CampaignError, enroll

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        result = await enroll(ts, campaign, body.contact_ids)
    except CampaignError as exc:
        raise _refuse(exc) from exc
    return {"added": result.added, "skipped": result.skipped, "warnings": result.warnings}


@router.post("/campaigns/{campaign_id}/draft")
async def draft_campaign(
    campaign_id: str, limit: int = 25,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    """Draft up to ``limit`` first emails. The screen calls again until nothing is left."""
    from nexus.engagement.sequences.service import CampaignError, draft_first_emails

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        return await draft_first_emails(ts, campaign, user_id=principal.user_id,
                                         limit=max(1, min(limit, 50)))
    except CampaignError as exc:
        raise _refuse(exc) from exc


@router.get("/campaigns/{campaign_id}/review", response_model=list[ReviewItemOut])
async def get_review(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[ReviewItemOut]:
    from nexus.engagement.sequences.service import review_queue

    campaign = await _campaign(ts, campaign_id, principal)
    rows = await review_queue(ts, campaign)
    contacts, accounts = await _people(ts, [e.contact_id for e, _m in rows],
                                       [e.account_id for e, _m in rows])
    return [ReviewItemOut(
        enrollment_id=e.id, contact_id=e.contact_id, message_id=getattr(m, "id", None),
        **_named(contacts.get(e.contact_id), accounts.get(e.account_id)),
        subject=getattr(m, "subject", "") or "", body=getattr(m, "body_text", "") or "",
        quality_problems=list(getattr(m, "quality_problems", None) or []),
        status=getattr(m, "status", "undrafted")) for e, m in rows]


async def _message(ts: TenantSession, message_id: str, principal: Principal):
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    message = await ts.get(EngagementMessage, message_id)
    if message is None or message.enrollment_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
    await _campaign(ts, enrollment.campaign_id, principal)
    return message


@router.post("/messages/{message_id}/approve", status_code=204, response_model=None)
async def approve_message(
    message_id: str, body: ApproveIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences.service import CampaignError, approve

    message = await _message(ts, message_id, principal)
    try:
        await approve(ts, message, user_id=principal.user_id, subject=body.subject, body=body.body)
    except CampaignError as exc:
        raise _refuse(exc) from exc


@router.post("/messages/{message_id}/regenerate", response_model=ReviewItemOut)
async def regenerate_message(
    message_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> ReviewItemOut:
    from nexus.engagement.sequences.service import CampaignError, regenerate

    message = await _message(ts, message_id, principal)
    try:
        await regenerate(ts, message, user_id=principal.user_id)
    except CampaignError as exc:
        raise _refuse(exc) from exc
    contacts, accounts = await _people(ts, [message.contact_id], [])
    contact = contacts.get(message.contact_id)
    return ReviewItemOut(enrollment_id=message.enrollment_id, contact_id=message.contact_id,
                         **_named(contact, accounts.get(getattr(contact, "account_id", None))),
                         message_id=message.id, subject=message.subject, body=message.body_text,
                         quality_problems=list(message.quality_problems or []),
                         status=message.status)


@router.post("/campaigns/{campaign_id}/approve-all")
async def approve_all(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import approve_all_passing

    campaign = await _campaign(ts, campaign_id, principal)
    return {"approved": await approve_all_passing(ts, campaign, user_id=principal.user_id)}


@router.get("/campaigns/{campaign_id}/estimate")
async def get_estimate(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sending.limits import sent_today, volume_warning
    from nexus.engagement.sequences.estimate import estimate
    from nexus.models.engagement import EngagementEnrollment, MailboxConnection

    campaign = await _campaign(ts, campaign_id, principal)
    result = await estimate(ts, campaign)
    mailbox = await ts.get(MailboxConnection, campaign.mailbox_connection_id)
    ready = len(await ts.list(EngagementEnrollment,
                              EngagementEnrollment.campaign_id == campaign.id,
                              EngagementEnrollment.status.in_(("awaiting_review", "active"))))
    today = await sent_today(ts, mailbox) if mailbox else 0
    return {**result.as_dict(),
            "volume_warning": volume_warning(getattr(mailbox, "email", ""), today, planned=ready)}


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> dict:
    from nexus.engagement.sequences.service import CampaignError, CreditGateRefused, launch

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        result = await launch(ts, campaign, user_id=principal.user_id)
    except CreditGateRefused as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                            {"detail": str(exc), "estimate": exc.estimate.as_dict()}) from exc
    except CampaignError as exc:
        raise _refuse(exc) from exc
    return {"status": campaign.status, "estimate": result.as_dict()}


@router.post("/campaigns/{campaign_id}/{action}", status_code=204, response_model=None)
async def campaign_action(
    campaign_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences import service

    campaign = await _campaign(ts, campaign_id, principal)
    try:
        if action == "pause":
            await service.pause_campaign(ts, campaign)
        elif action == "resume":
            await service.resume_campaign(ts, campaign, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_campaign(ts, campaign, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except service.CreditGateRefused as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except service.CampaignError as exc:
        raise _refuse(exc) from exc


@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[EnrollmentOut]:
    from nexus.models.engagement import EngagementEnrollment

    campaign = await _campaign(ts, campaign_id, principal)
    rows = await ts.list(EngagementEnrollment, EngagementEnrollment.campaign_id == campaign.id)
    contacts, accounts = await _people(ts, [e.contact_id for e in rows],
                                       [e.account_id for e in rows])
    return [EnrollmentOut(
        id=e.id, contact_id=e.contact_id, account_id=e.account_id,
        **_named(contacts.get(e.contact_id), accounts.get(e.account_id)), status=e.status,
        status_reason=e.status_reason, current_step_index=e.current_step_index,
        next_action_at=e.next_action_at, snoozed_until=e.snoozed_until,
        contact_timezone=e.contact_timezone) for e in rows]


async def _enrollment(ts: TenantSession, enrollment_id: str, principal: Principal):
    from nexus.models.engagement import EngagementEnrollment

    enrollment = await ts.get(EngagementEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    await _campaign(ts, enrollment.campaign_id, principal)
    return enrollment


@router.post("/enrollments/{enrollment_id}/move", status_code=204, response_model=None)
async def move_enrollment(
    enrollment_id: str, body: MoveIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences.service import CampaignError, move_next

    try:
        await move_next(ts, await _enrollment(ts, enrollment_id, principal), body.when)
    except CampaignError as exc:
        raise _refuse(exc) from exc


@router.post("/enrollments/{enrollment_id}/{action}", status_code=204, response_model=None)
async def enrollment_action(
    enrollment_id: str, action: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> None:
    from nexus.engagement.sequences import service

    enrollment = await _enrollment(ts, enrollment_id, principal)
    try:
        if action == "pause":
            await service.pause_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "resume":
            await service.resume_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "stop":
            await service.stop_enrollment(ts, enrollment, user_id=principal.user_id)
        elif action == "send-now":
            await service.send_now(ts, enrollment)
        elif action == "remove":
            await service.remove(ts, enrollment, user_id=principal.user_id)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown action")
    except service.CampaignError as exc:
        raise _refuse(exc) from exc
