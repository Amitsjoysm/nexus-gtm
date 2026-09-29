"""Lists: account lists and contact lists, built from a filter or by hand (`nexus/lists/`).

Everyone who can work accounts (rep+) can make a list and fill it. Changing one belongs to the
person who made it, or to a manager, who looks after the team's lists: a rep tidying up must not be
able to empty a colleague's list the day before their campaign starts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    AccountOut,
    ListBuildRequest,
    ListMemberOut,
    ListMembersIn,
    ListMembersPageOut,
    ListRenameIn,
    ProspectListOut,
)
from nexus.core.db import ensure_aware
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.lists import service
from nexus.lists.builder import get_list_builder
from nexus.models.workflow import ProspectList

router = APIRouter(tags=["lists"])


def _can_edit(principal: Principal, plist: ProspectList) -> bool:
    if plist.owner_user_id and plist.owner_user_id == principal.user_id:
        return True
    return has_permission(principal.role, Permission.manage_campaigns)  # manager+


async def _owner_names(ts: TenantSession, lists: list[ProspectList]) -> dict[str, str]:
    """Names through this workspace's memberships, so somebody who left shows no name."""
    owner_ids = {pl.owner_user_id for pl in lists if pl.owner_user_id}
    if not owner_ids:
        return {}
    from nexus.models.identity import Membership, User

    rows = (await ts.session.execute(
        select(User.id, User.full_name)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == ts.tenant_id, User.id.in_(owner_ids))
    )).all()
    return {user_id: name for user_id, name in rows}


async def _outs(ts: TenantSession, principal: Principal,
                lists: list[ProspectList]) -> list[ProspectListOut]:
    tally = await service.counts(ts, [pl.id for pl in lists])
    names = await _owner_names(ts, lists)
    return [
        ProspectListOut(
            id=pl.id, name=pl.name, kind=pl.kind or "account",
            members=tally.get(pl.id, (0, 0))[0], accounts=tally.get(pl.id, (0, 0))[1],
            owner_user_id=pl.owner_user_id, owner_name=names.get(pl.owner_user_id or ""),
            can_edit=_can_edit(principal, pl), created_at=ensure_aware(pl.created_at),
            updated_at=ensure_aware(pl.updated_at),
        )
        for pl in lists
    ]


async def _editable(ts: TenantSession, principal: Principal, list_id: str) -> ProspectList:
    plist = await service.get_live(ts, list_id)
    if plist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "List not found")
    if not _can_edit(principal, plist):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Only the person who made this list, or a manager, can change it.")
    return plist


@router.post("/lists/preview", response_model=list[AccountOut])
async def preview_list(
    body: ListBuildRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[AccountOut]:
    accounts = await get_list_builder().preview(ts, body.filter or {})
    return [
        AccountOut(
            id=a.id,
            name=a.name,
            domain=a.domain,
            industry=a.industry,
            employee_count=a.employee_count,
            country=a.country,
            tech_stack=a.tech_stack or [],
        )
        for a in accounts
    ]


@router.post("/lists", status_code=status.HTTP_201_CREATED)
async def create_list(
    body: ListBuildRequest,
    principal: Principal = Depends(require(Permission.manage_accounts)),
    ts: TenantSession = Depends(get_tenant_session),
) -> dict:
    if body.filter is not None:
        if body.kind != "account":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "A filter builds a list of companies; make a contact list by hand.")
        plist, count = await get_list_builder().build(
            ts, name=body.name, flt=body.filter, owner_user_id=principal.user_id
        )
        return {"id": plist.id, "name": plist.name, "kind": "account", "accounts": count,
                "members": count}
    try:
        plist = await service.create(ts, name=body.name, kind=body.kind,
                                     owner_user_id=principal.user_id)
        result = await service.add_members(ts, plist, account_ids=body.account_ids,
                                           contact_ids=body.contact_ids)
    except service.ListError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    accounts = (await service.counts(ts, [plist.id])).get(plist.id, (0, 0))[1]
    return {"id": plist.id, "name": plist.name, "kind": plist.kind, "accounts": accounts,
            "members": result.members, "added": result.added, "skipped": result.skipped}


@router.get("/lists", response_model=list[ProspectListOut])
async def list_saved_lists(
    kind: str | None = Query(default=None, pattern="^(account|contact)$"),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ProspectListOut]:
    """Live lists, newest first — feeds the Lists page, Add to list and the campaign picker."""
    return await _outs(ts, principal, await service.live_lists(ts, kind))


@router.get("/lists/{list_id}", response_model=ProspectListOut)
async def get_list(
    list_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ProspectListOut:
    plist = await service.get_live(ts, list_id)
    if plist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "List not found")
    return (await _outs(ts, principal, [plist]))[0]


@router.get("/lists/{list_id}/members", response_model=ListMembersPageOut)
async def list_members(
    list_id: str,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ListMembersPageOut:
    plist = await service.get_live(ts, list_id)
    if plist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "List not found")
    total, rows = await service.members(ts, plist, limit=limit, offset=offset, q=q)
    return ListMembersPageOut(total=total, items=[
        ListMemberOut(
            account_id=m.account_id, account_name=m.account_name, domain=m.domain,
            industry=m.industry, country=m.country, employee_count=m.employee_count,
            contact_id=m.contact_id, full_name=m.full_name, title=m.title, email=m.email,
            email_status=m.email_status, added_at=ensure_aware(m.added_at),
        )
        for m in rows
    ])


@router.patch("/lists/{list_id}", response_model=ProspectListOut)
async def rename_list(
    list_id: str,
    body: ListRenameIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ProspectListOut:
    plist = await _editable(ts, principal, list_id)
    try:
        await service.rename(ts, plist, body.name)
    except service.ListError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return (await _outs(ts, principal, [plist]))[0]


@router.delete("/lists/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_list(
    list_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """Archive: the members go, the row stays for the campaigns that point at it."""
    plist = await _editable(ts, principal, list_id)
    await service.archive(ts, plist)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/lists/{list_id}/members")
async def add_list_members(
    list_id: str,
    body: ListMembersIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    plist = await _editable(ts, principal, list_id)
    try:
        result = await service.add_members(ts, plist, account_ids=body.account_ids,
                                           contact_ids=body.contact_ids)
    except service.ListError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"added": result.added, "already": result.already, "skipped": result.skipped,
            "members": result.members}


@router.post("/lists/{list_id}/members/remove")
async def remove_list_members(
    list_id: str,
    body: ListMembersIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    plist = await _editable(ts, principal, list_id)
    removed = await service.remove_members(ts, plist, account_ids=body.account_ids,
                                           contact_ids=body.contact_ids)
    return {"removed": removed, "members": await service.member_count(ts, plist.id)}
