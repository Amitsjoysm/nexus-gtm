# nexus/api/routers/connections.py
"""Connected AI apps: the AI apps page and the OAuth consent screen (`/api/connections/...`).

The rows live in platform-global tables (``nexus/models/mcp.py`` says why), so there is no RLS
behind these endpoints: every query here filters on ``subject_tenant_id`` and, for anything a
member does to their own connections, on the user. A connection id from another workspace, or a
colleague's id when the caller may not act on it, is a 404, never a 403, so ids cannot be probed.

* Every member manages their own connections. Admins and owners (``manage_workspace``) also see the
  whole workspace's and can revoke any of them.
* Revoking is never gated on the plan or a switch: "stop this app" must always work.
* The API key secret is returned exactly once, by the create call, and is in no other response.
"""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus.api.deps import (
    Principal,
    get_tenant_session,
    require,
    require_platform_permission,
    require_writable,
)
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession
from nexus.mcp import credentials, oauth, urls
from nexus.models.mcp import McpConnection, OAuthClient

logger = logging.getLogger("nexus.api.routers.connections")

router = APIRouter(prefix="/connections", tags=["connections"])
admin_router = APIRouter(prefix="/admin/oauth-clients", tags=["admin"])

_Scope = Field(min_length=1, max_length=3)


class ConnectionOut(BaseModel):
    id: str
    kind: str
    name: str
    client_name: str = ""
    client_host: str = ""
    verified: bool = False
    scopes: list[str]
    key_prefix: str = ""
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    owner_name: str = ""
    owner_email: str = ""
    is_mine: bool = False


class KeyIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = _Scope
    expires_in_days: int | None = Field(default=90)


class KeyCreatedOut(BaseModel):
    connection: ConnectionOut
    secret: str
    server_url: str


class ConnectionPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=80)
    scopes: list[str] | None = Field(default=None, min_length=1, max_length=3)


class SetupTool(BaseModel):
    name: str
    title: str
    scope: str
    description: str


class SetupOut(BaseModel):
    server_url: str
    public_address_set: bool
    # False for localhost, private and internal addresses: Claude and ChatGPT connect from the
    # internet, so their connector steps cannot work there (apps on the user's computer still do).
    reachable_from_internet: bool
    tools: list[SetupTool]


class ConsentView(BaseModel):
    client_name: str
    client_uri: str = ""
    logo_uri: str = ""
    verified: bool
    redirect_host: str
    only_loopback: bool
    scopes: list[str]
    workspace_name: str = ""
    user_email: str = ""


class ConsentIn(BaseModel):
    model_config = {"extra": "forbid"}

    request: str = Field(min_length=10, max_length=4000)
    scopes: list[str] = Field(default_factory=list, max_length=3)


class RedirectOut(BaseModel):
    redirect_to: str


def _validated_scopes(scopes: list[str]) -> list[str]:
    cleaned = credentials.clean_scopes(scopes)
    if not cleaned or len(cleaned) != len(set(scopes)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "permissions must be some of: read, research, write")
    return cleaned


async def _gate(ts: TenantSession) -> None:
    """Refuse to CREATE a connection when the workspace cannot use AI apps (the same rule `/mcp`
    applies). Listing and revoking are never refused."""
    from nexus.billing.entitlements import preflight

    decision = await preflight(ts, "module.mcp")
    if decision.allowed:
        return
    ent = decision.entitlement
    message = (ent.switch_message if ent is not None and ent.switch_state else "") or (
        "AI apps are switched off right now." if ent is not None and ent.switch_state
        else "The workspace's plan does not include AI apps.")
    raise HTTPException(status.HTTP_403_FORBIDDEN, message)


async def _rows(ts: TenantSession, *, user_id: str | None) -> list[ConnectionOut]:
    from nexus.models.identity import User

    stmt = (
        select(McpConnection, User.full_name, User.email)
        .join(User, User.id == McpConnection.user_id)
        .where(McpConnection.subject_tenant_id == ts.tenant_id,
               McpConnection.revoked_at.is_(None))
        .order_by(McpConnection.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(McpConnection.user_id == user_id)
    rows = (await ts.session.execute(stmt)).all()
    client_ids = {c.client_id for c, _, _ in rows if c.client_id}
    clients: dict[str, OAuthClient] = {}
    if client_ids:
        clients = {c.client_id: c for c in (await ts.session.scalars(
            select(OAuthClient).where(OAuthClient.client_id.in_(client_ids)))).all()}
    return [_out(conn, clients.get(conn.client_id), name, email, user_id or "")
            for conn, name, email in rows]


def _out(conn: McpConnection, client: OAuthClient | None, owner_name: str = "",
         owner_email: str = "", me: str = "") -> ConnectionOut:
    return ConnectionOut(
        id=conn.id, kind=conn.kind, name=conn.name,
        client_name=(client.client_name if client else ""),
        client_host=(oauth.client_host(client) if client else ""),
        verified=(oauth.is_verified(client) if client else False),
        scopes=credentials.clean_scopes(conn.scopes), key_prefix=conn.key_prefix or "",
        created_at=conn.created_at, last_used_at=conn.last_used_at, expires_at=conn.expires_at,
        owner_name=owner_name or "", owner_email=owner_email or "",
        is_mine=bool(me) and conn.user_id == me,
    )


async def _connection_for(ts: TenantSession, principal: Principal, connection_id: str, *,
                          allow_admin: bool) -> McpConnection:
    conn = await ts.session.get(McpConnection, connection_id)
    if conn is None or conn.subject_tenant_id != ts.tenant_id or conn.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    mine = conn.user_id == principal.user_id
    admin = allow_admin and has_permission(Role(principal.role), Permission.manage_workspace)
    if not (mine or admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    view: str = Query(default="mine", pattern="^(mine|team)$"),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ConnectionOut]:
    if view == "team":
        if not has_permission(Role(principal.role), Permission.manage_workspace):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Role '{principal.role}' lacks manage_workspace")
        rows = await _rows(ts, user_id=None)
        for row in rows:
            row.is_mine = False
        mine_ids = {r.id for r in await _rows(ts, user_id=principal.user_id)}
        for row in rows:
            row.is_mine = row.id in mine_ids
        return rows
    return await _rows(ts, user_id=principal.user_id)


@router.get("/setup", response_model=SetupOut)
async def setup(
    request: Request,
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> SetupOut:
    from nexus.core.config import get_settings
    from nexus.mcp.tools import TOOLS, describe

    server_url = urls.mcp_url(request)
    return SetupOut(
        server_url=server_url,
        public_address_set=bool((get_settings().app_base_url or "").strip()),
        reachable_from_internet=urls.internet_reachable(server_url),
        tools=[SetupTool(name=t.name, title=t.title, scope=t.scope,
                         description=describe(t, {})["description"]) for t in TOOLS],
    )


@router.post("/keys", response_model=KeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: KeyIn,
    request: Request,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
    __: Principal = Depends(require_writable),
) -> KeyCreatedOut:
    from nexus.core.audit import record_audit

    if body.expires_in_days not in (None, 30, 90, 365):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "expires_in_days must be 30, 90, 365 or null (never)")
    scopes = _validated_scopes(body.scopes)
    await _gate(ts)
    conn, secret = await credentials.create_api_key(
        ts.session, user_id=principal.user_id, tenant_id=ts.tenant_id, name=body.name,
        scopes=scopes, expires_in_days=body.expires_in_days)
    await record_audit(ts, "mcp.key_created", actor_user_id=principal.user_id,
                       target_type="mcp_connection", target_id=conn.id,
                       meta={"name": conn.name, "scopes": scopes,
                             "expires_in_days": body.expires_in_days})
    return KeyCreatedOut(connection=_out(conn, None, me=principal.user_id), secret=secret,
                         server_url=urls.mcp_url(request))


@router.patch("/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: str,
    body: ConnectionPatch,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
    __: Principal = Depends(require_writable),
) -> ConnectionOut:
    from nexus.core.audit import record_audit

    conn = await _connection_for(ts, principal, connection_id, allow_admin=False)
    before = {"name": conn.name, "scopes": list(conn.scopes or [])}
    if body.name is not None:
        conn.name = body.name.strip()
    if body.scopes is not None:
        conn.scopes = _validated_scopes(body.scopes)
    await ts.session.flush()
    credentials.forget(conn.id)
    await record_audit(ts, "mcp.connection_changed", actor_user_id=principal.user_id,
                       target_type="mcp_connection", target_id=conn.id,
                       meta={"before": before, "after": {"name": conn.name,
                                                          "scopes": list(conn.scopes)}})
    client = None
    if conn.client_id:
        client = (await ts.session.scalars(select(OAuthClient).where(
            OAuthClient.client_id == conn.client_id))).first()
    return _out(conn, client, me=principal.user_id)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT,
               response_class=Response)
async def revoke_connection(
    connection_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
    __: Principal = Depends(require_writable),
) -> Response:
    from nexus.core.audit import record_audit
    from nexus.core.db import utcnow

    conn = await _connection_for(ts, principal, connection_id, allow_admin=True)
    conn.revoked_at = utcnow()
    conn.revoked_by = principal.user_id
    await oauth.revoke_connection_tokens(ts.session, conn.id)
    await ts.session.flush()
    credentials.forget(conn.id)
    await record_audit(ts, "mcp.connection_revoked", actor_user_id=principal.user_id,
                       target_type="mcp_connection", target_id=conn.id,
                       meta={"kind": conn.kind, "name": conn.name, "owner": conn.user_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- consent ---------------------------------------------------------------------------------

def _oauth_http(exc: oauth.OAuthError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.description)


@router.get("/oauth/request", response_model=ConsentView)
async def consent_request(
    request: str = Query(min_length=10, max_length=4000),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ConsentView:
    from nexus.models.identity import Tenant, User

    try:
        view = await oauth.request_view(request)
    except oauth.OAuthError as exc:
        raise _oauth_http(exc) from exc
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    user = await ts.session.get(User, principal.user_id)
    return ConsentView(**view, workspace_name=getattr(tenant, "name", "") or "",
                       user_email=getattr(user, "email", "") or "")


@router.post("/oauth/approve", response_model=RedirectOut)
async def consent_approve(
    body: ConsentIn,
    request: Request,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
    __: Principal = Depends(require_writable),
) -> RedirectOut:
    from nexus.core import metrics
    from nexus.core.audit import record_audit

    await _gate(ts)
    try:
        target = await oauth.approve(body.request, user_id=principal.user_id,
                                     tenant_id=ts.tenant_id, scopes=body.scopes,
                                     base=urls.base_url(request))
    except oauth.OAuthError as exc:
        metrics.record_oauth_event("consent", "refused")
        raise _oauth_http(exc) from exc
    metrics.record_oauth_event("consent", "approved")
    data = oauth.read_request(body.request)
    await record_audit(ts, "mcp.oauth_granted", actor_user_id=principal.user_id,
                       target_type="oauth_client", target_id=data["client_id"][:200],
                       meta={"scopes": credentials.clean_scopes(body.scopes),
                             "redirect_host": urlparse(data["redirect_uri"]).hostname})
    return RedirectOut(redirect_to=target)


@router.post("/oauth/deny", response_model=RedirectOut)
async def consent_deny(
    body: ConsentIn,
    request: Request,
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> RedirectOut:
    from nexus.core import metrics

    try:
        target = oauth.deny(body.request, urls.base_url(request))
    except oauth.OAuthError as exc:
        raise _oauth_http(exc) from exc
    metrics.record_oauth_event("consent", "denied")
    return RedirectOut(redirect_to=target)


# ---- pre-registered clients (superadmin) -----------------------------------------------------

class RegisteredClientIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)


class RegisteredClientOut(BaseModel):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    client_secret: str = ""


@admin_router.post("", response_model=RegisteredClientOut, status_code=status.HTTP_201_CREATED)
async def create_registered_client(
    body: RegisteredClientIn,
    _: Principal = Depends(require_platform_permission("providers.manage")),
) -> RegisteredClientOut:
    """A confidential OAuth client with a secret, e.g. for Anthropic-held credentials. The secret
    is shown once."""
    try:
        client, secret = await oauth.create_registered_client(body.name, body.redirect_uris)
    except oauth.OAuthError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.description) from exc
    return RegisteredClientOut(client_id=client.client_id, client_name=client.client_name,
                               redirect_uris=list(client.redirect_uris), client_secret=secret)


@admin_router.get("", response_model=list[RegisteredClientOut])
async def list_registered_clients(
    _: Principal = Depends(require_platform_permission("providers.manage")),
) -> list[RegisteredClientOut]:
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(OAuthClient).where(OAuthClient.kind == "registered")
                                .order_by(OAuthClient.created_at.desc()))).all()
    return [RegisteredClientOut(client_id=c.client_id, client_name=c.client_name,
                                redirect_uris=list(c.redirect_uris or [])) for c in rows]
