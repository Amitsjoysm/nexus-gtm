"""Rep workflow & automation endpoints: inbox, lists, plays, analytics, ingestion."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    AccountOut,
    ActivityItemOut,
    InboxTaskOut,
    ListBuildRequest,
    PlayIn,
    PlayOut,
    ProspectListOut,
    TriageOut,
)
from nexus.analytics.service import get_analytics_service
from nexus.core.db import ensure_aware, utcnow
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.inbox.service import get_inbox_service
from nexus.ingestion.service import get_ingestion_service
from nexus.lists.builder import get_list_builder
from nexus.models.account import Account
from nexus.models.workflow import ListItem, Play, ProspectList

router = APIRouter(tags=["workflow"])


# ---- inbox ----
@router.get("/inbox", response_model=list[InboxTaskOut])
async def list_inbox(
    principal: Principal = Depends(require(Permission.manage_accounts)),
    ts: TenantSession = Depends(get_tenant_session),
    mine: bool = False,
    status: str = "open",  # open | done | snoozed | all — lets reps recover past/pending work
    limit: int = 50,
) -> list[InboxTaskOut]:
    owner = principal.user_id if mine else None
    svc = get_inbox_service()
    tasks = await svc.list_tasks(ts, owner_user_id=owner, status=status, limit=limit)
    triage = await svc.triage(ts, tasks)
    now = utcnow()

    def _age_hours(t) -> float | None:
        created = ensure_aware(t.created_at)
        if created is None:
            return None
        return round(max(0.0, (now - created).total_seconds() / 3600), 1)

    return [
        InboxTaskOut(
            id=t.id,
            title=t.title,
            reason=t.reason,
            priority=t.priority,
            status=t.status,
            account_id=t.account_id,
            suggested_action=t.suggested_action or {},
            triage=TriageOut(**triage[t.id].as_dict()) if t.id in triage else None,
            created_at=t.created_at.isoformat() if t.created_at else None,
            age_hours=_age_hours(t),
        )
        for t in tasks
    ]


@router.post("/inbox/{task_id}/complete", response_model=InboxTaskOut)
async def complete_task(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> InboxTaskOut:
    task = await get_inbox_service().complete(ts, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return InboxTaskOut(
        id=task.id,
        title=task.title,
        reason=task.reason,
        priority=task.priority,
        status=task.status,
        account_id=task.account_id,
        suggested_action=task.suggested_action or {},
    )


@router.post("/inbox/{task_id}/reopen", response_model=InboxTaskOut)
async def reopen_task(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> InboxTaskOut:
    """Mark a completed task incomplete again (it returns to the open list)."""
    task = await get_inbox_service().reopen(ts, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return InboxTaskOut(
        id=task.id,
        title=task.title,
        reason=task.reason,
        priority=task.priority,
        status=task.status,
        account_id=task.account_id,
        suggested_action=task.suggested_action or {},
    )


# ---- lists ----
@router.post("/lists/preview", response_model=list[AccountOut])
async def preview_list(
    body: ListBuildRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[AccountOut]:
    accounts = await get_list_builder().preview(ts, body.filter)
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
async def build_list(
    body: ListBuildRequest,
    principal: Principal = Depends(require(Permission.manage_accounts)),
    ts: TenantSession = Depends(get_tenant_session),
) -> dict:
    plist, count = await get_list_builder().build(
        ts, name=body.name, flt=body.filter, owner_user_id=principal.user_id
    )
    return {"id": plist.id, "name": plist.name, "accounts": count}


@router.get("/lists", response_model=list[ProspectListOut])
async def list_saved_lists(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ProspectListOut]:
    """Saved segments with their current member counts — feeds the campaign list picker."""
    lists = await ts.list(ProspectList)
    counts = dict(
        (
            await ts.session.execute(
                select(ListItem.list_id, func.count())
                .where(ListItem.tenant_id == ts.tenant_id)
                .group_by(ListItem.list_id)
            )
        ).all()
    )
    lists.sort(key=lambda pl: pl.created_at, reverse=True)
    return [
        ProspectListOut(
            id=pl.id,
            name=pl.name,
            accounts=int(counts.get(pl.id, 0)),
            created_at=pl.created_at,
        )
        for pl in lists
    ]


# ---- plays ----
def _play_out(p: Play) -> PlayOut:
    return PlayOut(
        id=p.id, name=p.name, enabled=p.enabled, trigger=p.trigger or {}, actions=p.actions or []
    )


@router.get("/plays", response_model=list[PlayOut])
async def list_plays(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_plays)),
) -> list[PlayOut]:
    return [_play_out(p) for p in await ts.list(Play)]


@router.post("/plays", response_model=PlayOut, status_code=status.HTTP_201_CREATED)
async def create_play(
    body: PlayIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_plays)),
) -> PlayOut:
    play = Play(
        tenant_id=ts.tenant_id,
        name=body.name,
        enabled=body.enabled,
        trigger=body.trigger,
        actions=body.actions,
    )
    ts.add(play)
    await ts.flush()
    return _play_out(play)


# ---- ingestion ----
@router.post("/ingest/{account_id}")
async def ingest_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    signals = await get_ingestion_service().run_sources(ts, account)
    return {"account_id": account_id, "new_signals": [s.id for s in signals]}


# ---- analytics ----
@router.get("/analytics/overview")
async def analytics_overview(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.view_analytics)),
) -> dict:
    return await get_analytics_service().overview(ts)


@router.get("/analytics/activity", response_model=list[ActivityItemOut])
async def analytics_activity(
    limit: int = 20,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.view_analytics)),
) -> list[ActivityItemOut]:
    """Unified newest-first feed of recent workspace activity — the dashboard's live pulse."""
    items = await get_analytics_service().activity(ts, limit=limit)
    return [ActivityItemOut(**i) for i in items]
