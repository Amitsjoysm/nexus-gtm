"""The unified inbox: sent and received engagement email, one conversation per row (SDR round 2, 6.4).

Read-only, and dark with the rest of the engine. A rep reads their own mailboxes; a manager may ask
for the team's (`team=true`), as on the reply desk. Answering is the reply desk's: each thread names
the desk item to answer from, and no route here sends anything.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.routers.engagement_campaigns import _is_manager, require_campaigns_enabled
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/inbox", tags=["engagement"],
                   dependencies=[Depends(require_campaigns_enabled)])


class InboxThreadOut(BaseModel):
    thread_id: str
    subject: str
    snippet: str
    last_at: datetime | None
    last_direction: str
    message_count: int
    replied: bool
    unread: bool
    contact_id: str | None
    contact_name: str
    contact_email: str
    account_id: str | None
    account_name: str
    mailbox_id: str
    mailbox_email: str
    campaign_id: str | None
    campaign_name: str


class InboxOption(BaseModel):
    id: str
    label: str


class InboxListOut(BaseModel):
    threads: list[InboxThreadOut]
    #: The filters' choices, for the same scope as the threads (mine, or the team's).
    mailboxes: list[InboxOption]
    campaigns: list[InboxOption]
    team: bool


class InboxMessageOut(BaseModel):
    id: str
    direction: str
    status: str
    from_addr: str
    to_addrs: list[str]
    subject: str
    body: str
    #: The history an inbound reply quoted under itself, folded away on screen (2026-11-04).
    quoted: str = ""
    at: datetime | None
    category: str | None


class InboxThreadDetailOut(BaseModel):
    thread: InboxThreadOut
    messages: list[InboxMessageOut]
    #: Where to answer it: the reply desk item for the newest reply, and whether it is still open.
    reply_id: str | None
    reply_open: bool
    reply_link: str


@router.get("", response_model=InboxListOut)
async def list_threads(
    team: bool = False, mailbox_id: str | None = None, campaign_id: str | None = None,
    replied: bool = False, unread: bool = False, unanswered: bool = False,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> InboxListOut:
    from nexus.engagement.desk.inbox import threads, visible_mailboxes
    from nexus.models.engagement import EngagementCampaign

    whole_team = team and _is_manager(principal)
    mailboxes = await visible_mailboxes(ts, user_id=principal.user_id, team=whole_team)
    rows = await threads(ts, mailboxes=mailboxes, mailbox_id=mailbox_id, campaign_id=campaign_id,
                         replied=replied, unread=unread, unanswered=unanswered,
                         before=before, limit=limit)
    where = [] if whole_team else [EngagementCampaign.owner_user_id == principal.user_id]
    campaigns = sorted(await ts.list(EngagementCampaign, *where), key=lambda c: c.name or "")
    return InboxListOut(
        threads=[InboxThreadOut(**r.as_dict()) for r in rows],
        mailboxes=[InboxOption(id=m.id, label=m.email) for m in mailboxes],
        campaigns=[InboxOption(id=c.id, label=c.name) for c in campaigns],
        team=whole_team)


@router.get("/threads/{thread_id}", response_model=InboxThreadDetailOut)
async def read_thread(
    thread_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> InboxThreadDetailOut:
    from nexus.engagement.desk.inbox import thread, visible_mailboxes

    # A manager may open any thread on the team's mailboxes, whichever view they are in: the link
    # from a contact's history does not know which one that is.
    mailboxes = await visible_mailboxes(ts, user_id=principal.user_id,
                                        team=_is_manager(principal))
    view = await thread(ts, thread_id, mailboxes=mailboxes)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    link = ""
    if view.reply_id:
        link = (f"/engagement/mail?folder=needs_reply&reply={view.reply_id}" if view.reply_open
                else f"/engagement/mail?folder=handled&reply={view.reply_id}")
    return InboxThreadDetailOut(
        thread=InboxThreadOut(**view.row.as_dict()),
        messages=[InboxMessageOut(**m) for m in view.messages],
        reply_id=view.reply_id, reply_open=view.reply_open, reply_link=link)
