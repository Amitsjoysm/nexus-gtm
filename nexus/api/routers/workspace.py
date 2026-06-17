"""Workspace & member management: workspaces, invitations, role changes, removal.

All operations are tenant-scoped and require ``manage_workspace`` (admin/owner). Users are global
identities; a membership binds a user to this tenant with a role. The last owner is protected from
demotion/removal so a tenant can never be locked out.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    AutomationSettingsIn,
    AutomationSettingsOut,
    EmailAccountIn,
    EmailAccountOut,
    MailboxOut,
    EmailSettingsIn,
    EmailSettingsOut,
    EmailTestIn,
    EmailTestOut,
    MemberInviteRequest,
    MemberOut,
    MemberRoleUpdate,
    WorkspaceIn,
    WorkspaceOut,
)
from nexus.core.rbac import Permission
from nexus.core.security import hash_password
from nexus.core.tenancy import TenantSession
from nexus.models.identity import Membership, Tenant, User, Workspace

router = APIRouter(prefix="/workspace", tags=["workspace"])


# ---- workspaces ----
@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[WorkspaceOut]:
    return [WorkspaceOut(id=w.id, name=w.name) for w in await ts.list(Workspace)]


@router.post("/workspaces", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> WorkspaceOut:
    ws = Workspace(tenant_id=ts.tenant_id, name=body.name)
    ts.add(ws)
    await ts.flush()
    return WorkspaceOut(id=ws.id, name=ws.name)


@router.put("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def rename_workspace(
    workspace_id: str,
    body: WorkspaceIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> WorkspaceOut:
    ws = await ts.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    ws.name = body.name
    await ts.flush()
    return WorkspaceOut(id=ws.id, name=ws.name)


# ---- automation (continuous-automation opt-in) ----
@router.get("/automation", response_model=AutomationSettingsOut)
async def get_automation(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> AutomationSettingsOut:
    # Tenant is the isolation boundary itself (not TenantScoped); load via the raw session.
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return AutomationSettingsOut(automation_enabled=bool(tenant.automation_enabled))


@router.patch("/automation", response_model=AutomationSettingsOut)
async def set_automation(
    body: AutomationSettingsIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> AutomationSettingsOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    tenant.automation_enabled = body.automation_enabled
    await ts.flush()
    return AutomationSettingsOut(automation_enabled=tenant.automation_enabled)


# ---- outbound email (per-workspace SMTP: Gmail / Outlook) ----
def _email_settings_out(s: dict | None) -> EmailSettingsOut:
    s = s or {}
    return EmailSettingsOut(
        provider=s.get("provider", "gmail"),
        host=s.get("host", ""),
        port=int(s.get("port", 587)),
        username=s.get("username", ""),
        from_email=s.get("from_email", ""),
        from_name=s.get("from_name", ""),
        use_tls=bool(s.get("use_tls", True)),
        enabled=bool(s.get("enabled", False)),
        has_password=bool(s.get("password")),
        verified_at=s.get("verified_at"),
    )


@router.get("/email", response_model=EmailSettingsOut)
async def get_email_settings(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailSettingsOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return _email_settings_out(tenant.email_settings)


@router.put("/email", response_model=EmailSettingsOut)
async def set_email_settings(
    body: EmailSettingsIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailSettingsOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    current = dict(tenant.email_settings or {})
    updated = {
        "provider": body.provider,
        "host": body.host,
        "port": body.port,
        "username": body.username,
        "from_email": body.from_email,
        "from_name": body.from_name,
        "use_tls": body.use_tls,
        "enabled": body.enabled,
        # Password is write-only: a blank/omitted value keeps the stored secret.
        "password": body.password if body.password else current.get("password", ""),
        "verified_at": current.get("verified_at"),
    }
    tenant.email_settings = updated
    await ts.flush()
    return _email_settings_out(updated)


@router.post("/email/test", response_model=EmailTestOut)
async def test_email_settings(
    body: EmailTestIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailTestOut:
    """Send a test email through the configured SMTP, to the requester (or a given address)."""
    from nexus.integrations.email_sender import resolve_smtp, send_email

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    settings = dict(tenant.email_settings or {})
    cfg = resolve_smtp(settings)
    if not cfg["host"] or not cfg["username"] or not cfg["password"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SMTP is not fully configured")
    to = body.to
    if not to:
        user = await ts.session.get(User, principal.user_id)
        to = user.email if user else None
    if not to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No recipient for the test email")
    res = await send_email(
        settings, to=to,
        subject="NEXUS test email",
        body="This is a test from NEXUS. If you received it, your SMTP is set up correctly.",
    )
    if res.ok:
        settings["verified_at"] = _utcnow_iso()
        tenant.email_settings = settings
        await ts.flush()
    return EmailTestOut(ok=res.ok, detail=res.detail)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---- multiple sending mailboxes -------------------------------------------------------
import uuid as _uuid  # noqa: E402

from nexus.integrations.email_sender import (  # noqa: E402
    account_is_configured as _account_is_configured,
    list_accounts as _email_list_accounts,
    resolve_smtp as _resolve_smtp,
    send_email as _send_email,
)


def _account_out(a: dict) -> EmailAccountOut:
    return EmailAccountOut(
        id=a["id"],
        label=a.get("label", ""),
        provider=a.get("provider", "gmail"),
        host=a.get("host", ""),
        port=int(a.get("port", 587)),
        username=a.get("username", ""),
        from_email=a.get("from_email", "") or a.get("username", ""),
        from_name=a.get("from_name", ""),
        use_tls=bool(a.get("use_tls", True)),
        enabled=bool(a.get("enabled", True)),
        default=bool(a.get("default", False)),
        has_password=bool(a.get("password")),
        verified_at=a.get("verified_at"),
    )


def _load_accounts(tenant: Tenant) -> list[dict]:
    """Mutable account list, migrating a legacy single-account config on first touch."""
    settings = dict(tenant.email_settings or {})
    accts = settings.get("accounts")
    if isinstance(accts, list):
        return [dict(a) for a in accts]
    return [dict(a) for a in _email_list_accounts(settings)]  # [] or one migrated legacy acct


def _save_accounts(tenant: Tenant, accounts: list[dict]) -> None:
    settings = dict(tenant.email_settings or {})
    settings["accounts"] = accounts
    settings["enabled"] = any(a.get("enabled") for a in accounts)  # master switch follows accounts
    tenant.email_settings = settings


@router.get("/email/accounts", response_model=list[EmailAccountOut])
async def list_email_accounts(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> list[EmailAccountOut]:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    if accounts and "accounts" not in (tenant.email_settings or {}):
        _save_accounts(tenant, accounts)  # persist the legacy → accounts migration once
        await ts.flush()
    return [_account_out(a) for a in accounts]


@router.get("/email/mailboxes", response_model=list[MailboxOut])
async def list_send_mailboxes(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.approve_outreach)),
) -> list[MailboxOut]:
    """Send-ready mailboxes for the approval gate. Approvers (not only admins) may read these;
    only configured, enabled accounts are returned, and never any secret."""
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    return [
        MailboxOut(id=a["id"], label=a.get("label", "") or a.get("from_email", ""),
                   from_email=a.get("from_email", "") or a.get("username", ""),
                   default=bool(a.get("default")))
        for a in accounts
        if _account_is_configured(a)
    ]


@router.post("/email/accounts", response_model=EmailAccountOut, status_code=status.HTTP_201_CREATED)
async def add_email_account(
    body: EmailAccountIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailAccountOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    acct = {
        "id": _uuid.uuid4().hex[:12],
        "label": body.label or body.from_email or body.username or "Mailbox",
        "provider": body.provider,
        "host": body.host,
        "port": body.port,
        "username": body.username,
        "password": body.password or "",
        "from_email": body.from_email or body.username,
        "from_name": body.from_name,
        "use_tls": body.use_tls,
        "enabled": body.enabled,
        "default": not any(a.get("default") for a in accounts),  # first added becomes default
        "verified_at": None,
    }
    accounts.append(acct)
    _save_accounts(tenant, accounts)
    await ts.flush()
    return _account_out(acct)


@router.put("/email/accounts/{account_id}", response_model=EmailAccountOut)
async def update_email_account(
    account_id: str,
    body: EmailAccountIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailAccountOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    acct = next((a for a in accounts if a["id"] == account_id), None)
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    acct.update(
        {
            "label": body.label or acct.get("label", ""),
            "provider": body.provider,
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "from_email": body.from_email or body.username,
            "from_name": body.from_name,
            "use_tls": body.use_tls,
            "enabled": body.enabled,
            # Password is write-only: a blank/omitted value keeps the stored secret.
            "password": body.password if body.password else acct.get("password", ""),
        }
    )
    if body.password:
        acct["verified_at"] = None  # creds changed → must re-verify
    _save_accounts(tenant, accounts)
    await ts.flush()
    return _account_out(acct)


@router.delete("/email/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> Response:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    remaining = [a for a in accounts if a["id"] != account_id]
    if len(remaining) == len(accounts):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    if remaining and not any(a.get("default") for a in remaining):
        remaining[0]["default"] = True  # never leave the workspace without a default mailbox
    _save_accounts(tenant, remaining)
    await ts.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/email/accounts/{account_id}/default", response_model=EmailAccountOut)
async def set_default_email_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailAccountOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    target = next((a for a in accounts if a["id"] == account_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    for a in accounts:
        a["default"] = a["id"] == account_id
    _save_accounts(tenant, accounts)
    await ts.flush()
    return _account_out(target)


@router.post("/email/accounts/{account_id}/test", response_model=EmailTestOut)
async def test_email_account(
    account_id: str,
    body: EmailTestIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> EmailTestOut:
    """Send a test email from one specific mailbox, to the requester (or a given address)."""
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    accounts = _load_accounts(tenant)
    acct = next((a for a in accounts if a["id"] == account_id), None)
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    cfg = _resolve_smtp(acct)
    if not cfg["host"] or not cfg["username"] or not cfg["password"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SMTP is not fully configured")
    to = body.to
    if not to:
        user = await ts.session.get(User, principal.user_id)
        to = user.email if user else None
    if not to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No recipient for the test email")
    res = await _send_email(
        acct, to=to,
        subject="NEXUS test email",
        body="This is a test from NEXUS. If you received it, this mailbox is set up correctly.",
    )
    if res.ok:
        acct["verified_at"] = _utcnow_iso()
        _save_accounts(tenant, accounts)
        await ts.flush()
    return EmailTestOut(ok=res.ok, detail=res.detail)


# ---- members ----
def _member_out(m: Membership) -> MemberOut:
    return MemberOut(
        membership_id=m.id,
        user_id=m.user_id,
        email=m.user.email,
        full_name=m.user.full_name,
        role=m.role,
        workspace_id=m.workspace_id,
    )


async def _count_owners(ts: TenantSession) -> int:
    owners = await ts.list(Membership, Membership.role == "owner")
    return len(owners)


@router.get("/members", response_model=list[MemberOut])
async def list_members(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> list[MemberOut]:
    return [_member_out(m) for m in await ts.list(Membership)]


@router.post("/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: MemberInviteRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> MemberOut:
    # Reuse a global user if the email already exists, else provision one.
    user = (await ts.session.scalars(select(User).where(User.email == body.email))).first()
    if user is None:
        user = User(
            email=body.email,
            full_name=body.full_name,
            password_hash=hash_password(body.password),
        )
        ts.session.add(user)
        await ts.flush()

    existing = await ts.first(Membership, Membership.user_id == user.id)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is already a member of this tenant")

    membership = Membership(
        tenant_id=ts.tenant_id,
        user_id=user.id,
        workspace_id=body.workspace_id,
        role=body.role,
    )
    ts.add(membership)
    await ts.flush()
    await ts.refresh(membership)
    return _member_out(membership)


@router.put("/members/{membership_id}/role", response_model=MemberOut)
async def change_member_role(
    membership_id: str,
    body: MemberRoleUpdate,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> MemberOut:
    membership = await ts.get(Membership, membership_id)
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if membership.role == "owner" and body.role != "owner" and await _count_owners(ts) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot demote the last owner")
    membership.role = body.role
    await ts.flush()
    return _member_out(membership)


@router.delete("/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    membership_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> Response:
    membership = await ts.get(Membership, membership_id)
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if membership.role == "owner" and await _count_owners(ts) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot remove the last owner")
    await ts.delete(membership)
    await ts.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
