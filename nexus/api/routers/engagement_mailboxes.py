# nexus/api/routers/engagement_mailboxes.py
"""SDR mailboxes connected by OAuth: My mailboxes (spec §9, D1, D2).

Every member can connect their own mailbox (``run_engagement``); managers can list the team's and
disconnect one (``manage_engagement``), for the day someone leaves. Only the owner edits a mailbox's
timezone, signature and confidence bar: those describe the person, not the team.

The OAuth callback carries no bearer token — the browser arrives from Google or Microsoft — so the
signed ``state`` is the credential, exactly as in ``routers/network.py``. Every outcome is a redirect
back to ``/mailboxes`` with ``connected=`` or ``error=``; nothing on that path renders a stack trace.

No token ever appears in a response model.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import MailboxConnection

logger = logging.getLogger("nexus.api.engagement_mailboxes")

router = APIRouter(prefix="/engagement/mailboxes", tags=["engagement"])

PROVIDERS = ("google", "microsoft")


class MailboxOut(BaseModel):
    id: str
    provider: str
    email: str
    display_name: str
    owner_user_id: str
    mine: bool
    status: str
    last_error: str | None
    timezone: str
    signature: str
    reply_confidence: float | None
    effective_reply_confidence: float
    reply_confidence_min: float
    reply_confidence_max: float
    paused_until: datetime | None
    last_synced_at: datetime | None
    created_at: datetime
    # Sent since the owner's local midnight, and the warning above 50 (D10). Never a block.
    sent_today: int = 0
    volume_warning: str = ""


class ProviderStateOut(BaseModel):
    provider: str
    configured: bool


class StartIn(BaseModel):
    model_config = {"extra": "forbid"}

    timezone: str = Field(default="UTC", max_length=64)


class StartOut(BaseModel):
    authorize_url: str


class MailboxPatch(BaseModel):
    model_config = {"extra": "forbid"}

    timezone: str | None = Field(default=None, max_length=64)
    signature: str | None = Field(default=None, max_length=4000)
    reply_confidence: float | None = None
    #: Explicitly clear the SDR's own bar and fall back to the workspace default.
    clear_reply_confidence: bool = False


async def _settings(ts: TenantSession):
    from nexus.engagement.settings import read_settings
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return read_settings(getattr(tenant, "email_settings", None))


async def _out(ts: TenantSession, row: MailboxConnection, principal: Principal) -> MailboxOut:
    from nexus.engagement.sending.limits import sent_today, volume_warning
    from nexus.engagement.settings import effective_confidence

    settings = await _settings(ts)
    today = await sent_today(ts, row)
    return MailboxOut(
        id=row.id, provider=row.provider, email=row.email, display_name=row.display_name or "",
        owner_user_id=row.owner_user_id, mine=row.owner_user_id == principal.user_id,
        status=row.status, last_error=row.last_error, timezone=row.timezone or "UTC",
        signature=row.signature or "", reply_confidence=row.reply_confidence,
        effective_reply_confidence=effective_confidence(settings, row.reply_confidence),
        reply_confidence_min=settings.reply_confidence_min,
        reply_confidence_max=settings.reply_confidence_max,
        paused_until=row.paused_until, last_synced_at=row.last_synced_at,
        created_at=row.created_at, sent_today=today,
        volume_warning=volume_warning(row.email, today),
    )


async def _row(ts: TenantSession, mailbox_id: str) -> MailboxConnection:
    row = await ts.get(MailboxConnection, mailbox_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    return row


def _is_manager(principal: Principal) -> bool:
    from nexus.core.rbac import Role

    return has_permission(Role(principal.role), Permission.manage_engagement)


@router.get("", response_model=list[MailboxOut])
async def list_mailboxes(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[MailboxOut]:
    from nexus.engagement.mailboxes.service import list_mailboxes as _list

    if team and not _is_manager(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers can see the team's mailboxes")
    rows = await _list(ts, user_id=principal.user_id, team=team)
    return [await _out(ts, r, principal) for r in rows]


@router.get("/providers", response_model=list[ProviderStateOut])
async def provider_states(
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[ProviderStateOut]:
    from nexus.engagement.config import oauth_app

    return [ProviderStateOut(provider=p, configured=(await oauth_app(p)).configured)
            for p in PROVIDERS]


@router.post("/oauth/{provider}/start", response_model=StartOut)
async def start_connect(
    provider: str,
    body: StartIn,
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> StartOut:
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown mailbox provider")
    app = await oauth_app(provider)
    if not app.configured:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": f"{provider.title()} mailboxes are not set up for this deployment yet. "
                        "Ask your administrator.", "missing": list(app.missing)},
        )
    url = oauth.start(app, user_id=principal.user_id, tenant_id=principal.tenant_id,
                      timezone=body.timezone)
    return StartOut(authorize_url=url)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.engagement.config import oauth_app, public_base_url
    from nexus.engagement.mailboxes import oauth
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.mailboxes.registry import make_provider
    from nexus.engagement.mailboxes.service import MailboxOwnedByColleague, upsert_connection
    from nexus.models.identity import Membership

    def _to(query: str) -> RedirectResponse:
        return RedirectResponse(f"{public_base_url()}/mailboxes?{query}", status_code=302)

    if provider not in PROVIDERS:
        return _to("error=unknown_provider")
    if error or not code or not state:
        return _to(f"error={'denied' if error == 'access_denied' else 'oauth_failed'}")
    claims = oauth.verify_state(state, provider=provider)
    if claims is None:
        return _to("error=bad_state")
    app = await oauth_app(provider)
    if not app.configured:
        return _to("error=not_configured")
    try:
        data = await oauth.exchange_code(app, code=code, verifier=claims["pkce"])
    except ProviderError:
        logger.warning("mailbox OAuth code exchange failed for %s", provider, exc_info=True)
        return _to("error=exchange_failed")
    if oauth.missing_scopes(app, data.get("scope", "")):
        return _to("error=missing_scopes")
    bundle = oauth.bundle_from_response(data)
    try:
        profile = await make_provider(provider, access_token=bundle["access_token"]).profile()
    except ProviderError:
        return _to("error=profile_failed")
    if not profile.email:
        return _to("error=profile_failed")

    tenant_id, user_id = claims["tid"], claims["uid"]
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        member = (await session.scalars(select(Membership).where(
            Membership.tenant_id == tenant_id, Membership.user_id == user_id,
        ))).first()
        if member is None:
            return _to("error=not_a_member")
        ts = TenantSession(session, tenant_id)
        try:
            await upsert_connection(
                ts, owner_user_id=user_id, provider=provider, email=profile.email,
                display_name=profile.display_name, bundle=bundle,
                scopes=(data.get("scope") or "").split(), timezone=claims.get("tz", "UTC"),
            )
            await session.commit()
        except MailboxOwnedByColleague:
            await session.rollback()
            return _to("error=owned_by_colleague")
    return _to(f"connected={provider}")


@router.patch("/{mailbox_id}", response_model=MailboxOut)
async def update_mailbox(
    mailbox_id: str,
    body: MailboxPatch,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> MailboxOut:
    from nexus.engagement.mailboxes import service

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the mailbox's owner can change it")
    changes: dict = {}
    if body.timezone is not None:
        changes["timezone"] = body.timezone
    if body.signature is not None:
        changes["signature"] = body.signature
    if body.clear_reply_confidence:
        changes["reply_confidence"] = None
    elif body.reply_confidence is not None:
        changes["reply_confidence"] = body.reply_confidence
    try:
        await service.update_mailbox(ts, row, **changes)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, row, principal)


@router.post("/{mailbox_id}/check", response_model=MailboxOut)
async def check_mailbox(
    mailbox_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> MailboxOut:
    from nexus.engagement.mailboxes import service
    from nexus.engagement.mailboxes.provider import ProviderError

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the mailbox's owner can check it")
    try:
        await service.check_connection(ts, row)
    except ProviderError:
        pass  # the row now carries the status and the provider's words; the screen shows them
    return await _out(ts, row, principal)


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mailbox(
    mailbox_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> Response:
    from nexus.engagement.mailboxes import service

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id and not _is_manager(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Only the owner or a manager can disconnect a mailbox")
    await service.disconnect(ts, row, actor_user_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
