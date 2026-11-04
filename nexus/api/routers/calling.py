"""Cold-calling API: the call queue, AI scripts, and disposition logging.

Every endpoint is tenant-scoped (TenantSession) and RBAC-gated on ``manage_accounts`` — the same
gate as accounts/contacts. Additive: nothing here touches the email/cadence send paths.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    CallActivityOut,
    CallBriefOut,
    CallScriptOut,
    CallTaskOut,
    CreateCallTaskIn,
    DialIn,
    DialOut,
    DispositionIn,
    TelephonyStatusOut,
)
from nexus.calling.provider import (
    AgentNumberRequired,
    InvalidPhoneNumber,
    TelephonyError,
    TelephonyNotConfigured,
    TelephonyNotImplemented,
)
from nexus.calling.service import get_call_queue_service
from nexus.core.config import get_settings
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
        provider_call_id=a.provider_call_id, recording_url=a.recording_url,
        transcript=a.transcript,
    )


def _telephony_http_error(exc: TelephonyError) -> HTTPException:
    """Map a telephony failure to the status that tells the caller what to do about it.

    A bad number or a missing rep phone is the request's fault (422); missing credentials or an
    unknown provider is the deployment's (503); anything else means the provider refused or was
    unreachable (502). Never 200 — a call that did not connect must not read as success.
    """
    if isinstance(exc, (InvalidPhoneNumber, AgentNumberRequired)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if isinstance(exc, (TelephonyNotConfigured, TelephonyNotImplemented)):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


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
    principal: Principal = Depends(require(Permission.manage_accounts)),
    refresh: bool = False,
) -> CallScriptOut:
    """Today's script for this call, generated only when there is none. ``refresh=true`` is the
    console's Regenerate button — the one path that always writes a new script."""
    task = await ts.get(CallTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    script = await get_call_queue_service().generate_script(
        ts, task, refresh=refresh, opened_by=principal.user_id)
    return CallScriptOut(**{k: script.get(k) for k in CallScriptOut.model_fields if script.get(k) is not None})


@router.get("/tasks/{task_id}/campaign-script")
async def campaign_script(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """The team's own script for this call's campaign, as written, with only its marked
    placeholders filled (`nexus/calling/scripts.py`). 404 when the call has no campaign or the
    campaign no script, so the console shows the AI talk track instead."""
    from nexus.calling.scripts import fill_placeholders
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    task = await ts.get(CallTask, task_id)
    enrollment = await ts.get(EngagementEnrollment, task.engagement_enrollment_id) \
        if task is not None and task.engagement_enrollment_id else None
    campaign = await ts.get(EngagementCampaign, enrollment.campaign_id) if enrollment else None
    if campaign is None or not (campaign.call_script or "").strip():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This call has no campaign script")
    values = await _placeholder_values(ts, task, principal.user_id)
    meta = dict(campaign.call_script_meta or {})
    return {"campaign_id": campaign.id, "campaign_name": campaign.name,
            "filename": meta.get("filename"),
            "text": fill_placeholders(campaign.call_script, values)}


async def _placeholder_values(ts: TenantSession, task: CallTask, user_id: str | None) -> dict:
    """What a script's `{{placeholders}}` become for this call (`calling/scripts.PLACEHOLDERS`):
    the person, their company, the caller's first name and the strongest citable signal."""
    from nexus.agents.facts import citable_signals
    from nexus.models.identity import User
    from nexus.models.signal import SignalEvent

    contact = await ts.get(Contact, task.contact_id) if task.contact_id else None
    account = await ts.get(Account, task.account_id)
    signals = citable_signals(list((await ts.session.scalars(
        ts.select(SignalEvent, SignalEvent.account_id == task.account_id)
        .order_by(SignalEvent.occurred_at.desc()).limit(30))).all()))
    trigger = max(signals, key=lambda s: s.strength or 0, default=None)
    me = await ts.session.get(User, user_id) if user_id else None
    full_name = getattr(contact, "full_name", "") or ""
    return {
        "first_name": full_name.split()[0] if full_name.split() else "",
        "full_name": full_name, "title": getattr(contact, "title", "") or "",
        "company": getattr(account, "name", "") or "",
        "my_name": ((getattr(me, "full_name", "") or "").split() or [""])[0],
        "trigger": getattr(trigger, "title", "") or "",
    }


# ---- the SDR's own script (2026-11-04) ---------------------------------------------------------
#
# "Upload a script which is visible to the SDR during calls." One per person per workspace,
# uploaded from the call console, stored as written, shown for every call that person makes.

def _my_script_out(row) -> dict:
    meta = dict(row.meta or {})
    return {"filename": meta.get("filename"), "uploaded_at": meta.get("uploaded_at"),
            "text": row.script}


async def _my_script_row(ts: TenantSession, user_id: str):
    from nexus.models.calling import UserCallScript

    return await ts.first(UserCallScript, UserCallScript.user_id == user_id)


@router.get("/my-script")
async def get_my_script(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """The signed-in SDR's own script, as written. 404 when they have not uploaded one."""
    row = await _my_script_row(ts, principal.user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You have not uploaded a call script")
    return _my_script_out(row)


@router.post("/my-script/file")
async def put_my_script(
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """Upload or replace the SDR's own script (.docx or .txt), read with the campaign script's
    reader, so the same files work and the same ones are refused (422 with the reason)."""
    from nexus.calling.scripts import MAX_BYTES, UnreadableScript, read_document
    from nexus.core.db import utcnow
    from nexus.models.calling import UserCallScript

    data = await file.read(MAX_BYTES + 1)
    try:
        text = read_document(file.filename or "", data)
    except UnreadableScript as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    meta = {"filename": (file.filename or "")[:200], "uploaded_at": utcnow().isoformat()}
    row = await _my_script_row(ts, principal.user_id)
    if row is None:
        row = UserCallScript(tenant_id=ts.tenant_id, user_id=principal.user_id, script=text,
                             meta=meta)
        ts.add(row)
    else:
        row.script, row.meta = text, meta
    await ts.flush()
    return _my_script_out(row)


@router.delete("/my-script", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_my_script(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> None:
    """Remove the SDR's own script; their calls show the campaign's or the AI's again."""
    row = await _my_script_row(ts, principal.user_id)
    if row is not None:
        await ts.session.delete(row)
        await ts.flush()


@router.get("/tasks/{task_id}/my-script")
async def my_script_for_call(
    task_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """The SDR's own script for this call, with its marked placeholders filled. 404 when the
    call does not exist or they have no script, so the console shows what it showed before."""
    from nexus.calling.scripts import fill_placeholders

    task = await ts.get(CallTask, task_id)
    row = await _my_script_row(ts, principal.user_id) if task is not None else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No call script of yours for this call")
    out = _my_script_out(row)
    out["text"] = fill_placeholders(row.script, await _placeholder_values(ts, task,
                                                                          principal.user_id))
    return out


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


@router.get("/telephony", response_model=TelephonyStatusOut)
async def telephony_status(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> TelephonyStatusOut:
    """Can this deployment place live calls? Drives which control the call console shows.

    Reports only *whether* credentials are present — never their values. A selected-but-broken
    provider is reported here rather than raised, so the console can fall back to click-to-dial
    and tell the admin what to fix.
    """
    from nexus.calling import connection

    settings = get_settings()
    name = (settings.telephony_provider or "stub").strip().lower()
    try:
        resolved = await connection.resolve_call_provider(ts)
    except TelephonyError as exc:
        return TelephonyStatusOut(
            provider=name, mode="manual", from_number="", configured=False, detail=str(exc),
        )

    provider, from_number = resolved.provider, resolved.from_number
    live = provider.name != "stub"
    detail = None
    if live and not from_number:
        detail = (
            "Your Twilio connection has no caller ID. Add one under Integrations."
            if resolved.source == "workspace"
            else "No caller ID is set for calls. A superadmin sets it under Runtime settings, "
            "or NEXUS_TELEPHONY_FROM_NUMBER in the environment."
        )
    return TelephonyStatusOut(
        provider=provider.name,
        mode="live" if live and not detail else "manual",
        from_number=from_number,
        configured=live and not detail,
        record_calls=bool(getattr(provider, "record_calls", False)),
        detail=detail,
        source=resolved.source,
    )


@router.post("/tasks/{task_id}/dial", response_model=DialOut)
async def dial_call_task(
    task_id: str,
    body: DialIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> DialOut:
    """Start the call.

    Under the default stub nothing is dialled and a ``tel:`` URL comes back for the rep's own
    phone. Under a live provider this places a real, billable call — send an ``Idempotency-Key``
    header so a double-submit replays the first response instead of ringing the prospect twice.
    """
    task = await ts.get(CallTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    try:
        handle = await get_call_queue_service().place_call(
            ts, task_id, agent_number=body.agent_number, user_id=principal.user_id
        )
    except TelephonyError as exc:
        raise _telephony_http_error(exc) from None
    if handle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call task not found")
    return DialOut(
        mode=handle.mode, dial_url=handle.dial_url,
        provider_call_id=handle.provider_call_id,
    )


@router.get("/dispositions")
async def list_dispositions(
    channel: str = "call",
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[dict]:
    """The team's categories and dispositions for one channel, for the Category -> Disposition
    dropdowns (`nexus/calling/dispositions.py`)."""
    from nexus.calling.dispositions import CALL, EMAIL, for_channel

    if channel not in (CALL, EMAIL):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "channel is call or email")
    return for_channel(channel)


@router.post("/tasks/{task_id}/disposition", response_model=CallActivityOut)
async def log_disposition(
    task_id: str,
    body: DispositionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> CallActivityOut:
    from nexus.calling.dispositions import CALL, lookup

    chosen = lookup(body.disposition)
    if body.disposition not in ALL_DISPOSITIONS or chosen is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown disposition '{body.disposition}'")
    if CALL not in chosen.channels:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"'{chosen.label}' is not a call outcome.")
    activity = await get_call_queue_service().log_disposition(
        ts, task_id, disposition=body.disposition, notes=body.notes,
        duration_s=body.duration_s, next_step=body.next_step,
        provider_call_id=body.provider_call_id, user_id=principal.user_id,
        follow_up_at=body.follow_up_at,
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
