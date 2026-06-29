"""Cold-calling API: the call queue, AI scripts, and disposition logging.

Every endpoint is tenant-scoped (TenantSession) and RBAC-gated on ``manage_accounts`` — the same
gate as accounts/contacts. Additive: nothing here touches the email/cadence send paths.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    CallActivityOut,
    CallBriefOut,
    CallScriptOut,
    CallTaskOut,
    CreateCallTaskIn,
    DispositionIn,
)
from nexus.calling.service import get_call_queue_service
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.calling import ALL_DISPOSITIONS, CallTask

router = APIRouter(prefix="/calling", tags=["calling"])


def _activity_out(a) -> CallActivityOut:
    return CallActivityOut(
        id=a.id, call_task_id=a.call_task_id, account_id=a.account_id,
        contact_id=a.contact_id, disposition=a.disposition, notes=a.notes or "",
        duration_s=a.duration_s, next_step=a.next_step,
        occurred_at=a.occurred_at.isoformat() if a.occurred_at else "",
    )


@router.get("/queue", response_model=list[CallTaskOut])
async def call_queue(
    status_: str = "open",
    mine: bool = False,
    limit: int = 50,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> list[CallTaskOut]:
    """The prioritized call list, joined to account + contact (name/phone). ``mine`` filters to
    the caller's owned tasks. Tenant scoping is enforced in the query (both sides of the join)."""
    owner = principal.user_id if mine else None
    where = [CallTask.tenant_id == ts.tenant_id, CallTask.status == status_]
    if owner:
        where.append(CallTask.owner_user_id == owner)
    stmt = (
        select(CallTask, Account.name, Contact.full_name, Contact.title, Contact.phone)
        .join(Account, Account.id == CallTask.account_id)
        .outerjoin(Contact, Contact.id == CallTask.contact_id)
        .where(*where, Account.tenant_id == ts.tenant_id)
        .order_by(CallTask.priority.desc())
        .limit(limit)
    )
    rows = (await ts.session.execute(stmt)).all()
    return [
        CallTaskOut(
            id=t.id, account_id=t.account_id, account_name=acc_name,
            contact_id=t.contact_id, contact_name=full_name, title=title, phone=phone,
            reason=t.reason or "", priority=t.priority, status=t.status, source=t.source,
            due_at=t.due_at.isoformat() if t.due_at else None,
            has_script=bool(t.script_cache),
        )
        for (t, acc_name, full_name, title, phone) in rows
    ]


@router.post("/tasks", response_model=CallTaskOut, status_code=status.HTTP_201_CREATED)
async def create_call_task(
    body: CreateCallTaskIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CallTaskOut:
    account = await ts.get(Account, body.account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    contact = await ts.get(Contact, body.contact_id) if body.contact_id else None
    task = await get_call_queue_service().enqueue(
        ts, account_id=account.id, contact_id=contact.id if contact else None,
        reason=body.reason, priority=body.priority,
    )
    return CallTaskOut(
        id=task.id, account_id=account.id, account_name=account.name,
        contact_id=contact.id if contact else None,
        contact_name=contact.full_name if contact else None,
        title=contact.title if contact else None,
        phone=contact.phone if contact else None,
        reason=task.reason or "", priority=task.priority, status=task.status,
        source=task.source, has_script=bool(task.script_cache),
    )


@router.post("/tasks/{task_id}/script", response_model=CallScriptOut)
async def generate_call_script(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CallScriptOut:
    task = await ts.get(CallTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    script = await get_call_queue_service().generate_script(ts, task)
    return CallScriptOut(**{k: script.get(k) for k in CallScriptOut.model_fields if script.get(k) is not None})


@router.get("/tasks/{task_id}/brief", response_model=CallBriefOut)
async def call_brief(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CallBriefOut:
    """The pre-call research dossier for this task: the person, their company, the buying signals
    (each with its source), social insights, and grounded talking points — so the SDR is fully
    researched before dialing. Read-only; composes data that already exists."""
    task = await ts.get(CallTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    data = await get_call_queue_service().build_brief(ts, task)
    return CallBriefOut(**data)


@router.post("/tasks/{task_id}/disposition", response_model=CallActivityOut)
async def log_disposition(
    task_id: str,
    body: DispositionIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CallActivityOut:
    if body.disposition not in ALL_DISPOSITIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown disposition '{body.disposition}'")
    activity = await get_call_queue_service().log_disposition(
        ts, task_id, disposition=body.disposition, notes=body.notes,
        duration_s=body.duration_s, next_step=body.next_step,
    )
    if activity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    return _activity_out(activity)


@router.post("/tasks/{task_id}/skip", response_model=CallTaskOut)
async def skip_call_task(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CallTaskOut:
    task = await get_call_queue_service().skip(ts, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    account = await ts.get(Account, task.account_id)
    contact = await ts.get(Contact, task.contact_id) if task.contact_id else None
    return CallTaskOut(
        id=task.id, account_id=task.account_id,
        account_name=account.name if account else "",
        contact_id=task.contact_id, contact_name=contact.full_name if contact else None,
        title=contact.title if contact else None, phone=contact.phone if contact else None,
        reason=task.reason or "", priority=task.priority, status=task.status,
        source=task.source, has_script=bool(task.script_cache),
    )


@router.get("/contacts/{contact_id}/activities", response_model=list[CallActivityOut])
async def contact_call_activities(
    contact_id: str,
    limit: int = 50,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[CallActivityOut]:
    acts = await get_call_queue_service().list_activities(ts, contact_id=contact_id, limit=limit)
    return [_activity_out(a) for a in acts]
