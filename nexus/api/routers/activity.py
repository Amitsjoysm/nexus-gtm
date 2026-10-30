"""Call and email history for one contact or one account (SDR round 2, 5.2).

A router of its own rather than two more routes in `contacts.py` and `accounts.py`, which other work
is changing at the same time; the paths are the ones a reader expects. Readable by anyone who can
open the contact or the account (`manage_accounts`, as `GET /contacts/{id}` is), whatever the
engagement engine's switch says: the switch stops sending, and what was already said is history.
Another workspace's id, or a removed contact, answers 404.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession

router = APIRouter(tags=["activity"])


class ActivityOut(BaseModel):
    id: str
    kind: str
    at: datetime | None
    contact_id: str | None
    contact_name: str
    by: str
    category: str
    disposition: str
    disposition_code: str
    notes: str
    duration_s: int | None
    placed_live: bool
    direction: str
    status: str
    subject: str
    preview: str
    mailbox: str
    campaign_id: str | None
    campaign_name: str
    thread_id: str | None
    thread_link: str
    reply_category: str | None


def _manager(principal: Principal) -> bool:
    return has_permission(Role(principal.role), Permission.manage_engagement)


@router.get("/contacts/{contact_id}/activity", response_model=list[ActivityOut])
async def contact_activity(
    contact_id: str,
    limit: int = Query(default=100, ge=1, le=300),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ActivityOut]:
    from nexus.activity.history import for_contact
    from nexus.models.account import Contact

    contact = await ts.get(Contact, contact_id)
    if contact is None or getattr(contact, "deleted_at", None) is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    rows = await for_contact(ts, contact, viewer_id=principal.user_id,
                             manager=_manager(principal), limit=limit)
    return [ActivityOut(**r.as_dict()) for r in rows]


@router.get("/accounts/{account_id}/activity", response_model=list[ActivityOut])
async def account_activity(
    account_id: str,
    limit: int = Query(default=100, ge=1, le=300),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ActivityOut]:
    from nexus.activity.history import for_account
    from nexus.models.account import Account

    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    rows = await for_account(ts, account, viewer_id=principal.user_id,
                             manager=_manager(principal), limit=limit)
    return [ActivityOut(**r.as_dict()) for r in rows]
