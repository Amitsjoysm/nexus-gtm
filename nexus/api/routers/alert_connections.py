# nexus/api/routers/alert_connections.py
"""Connect a workspace's own Slack, Teams, Telegram or alert inbox.

Until this existed the channels were deployment env vars, so a customer could not connect theirs —
and an operator who set one to help a single customer would have routed every tenant's alerts into
it. See ``nexus/alerts/connections.py`` for why that is the CRM credential bug in a new place.

Workspace-admin level (`manage_workspace`), not rep-level. A workspace has ONE Slack per
``uq_integration_connection_tenant_kind``, so connecting is a workspace decision; choosing which of
your own alerts go there is the rep-level thing, and that lives in ``routers/notifications.py``.

**The secret is in no response model.** `ChannelOut` is the single place connection state becomes
JSON, which is what makes "the webhook URL never leaves the server" checkable rather than asserted
— the same rule as `_connection_out` in the CRM router.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from nexus.alerts.connections import (
    CHANNEL_FIELDS,
    clear_connection,
    connection_states,
    save_connection,
)
from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.redact import redact
from nexus.core.tenancy import TenantSession
from nexus.models.integration import ALERT_CHANNEL_KINDS

router = APIRouter(prefix="/alert-connections", tags=["alerts"])


class ChannelOut(BaseModel):
    """Connection state for one channel. Deliberately carries NO secret."""

    kind: str
    connected: bool
    #: A stored row whose secret no longer decrypts. Its own state, because a customer who
    #: connected Slack last month and sees a tick while nothing arrives has no way to diagnose it.
    needs_reconnect: bool = False
    status: str = "not_connected"
    verified_at: str | None = None
    last_error: str = ""
    #: Which fields the connect form must collect for this channel.
    fields: list[str] = Field(default_factory=list)


class ChannelsOut(BaseModel):
    channels: list[ChannelOut] = Field(default_factory=list)


class ConnectIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: Slack/Teams incoming webhook URL.
    url: str = ""
    #: Telegram.
    bot_token: str = ""
    chat_id: str = ""
    #: Email — where this workspace's alerts should land.
    to: str = ""


def _require_kind(kind: str) -> str:
    if kind not in ALERT_CHANNEL_KINDS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown alert channel: {kind}"
        )
    return kind


@router.get("", response_model=ChannelsOut)
async def list_connections(
    ts: TenantSession = Depends(get_tenant_session),
    # Rep-level READ: the notification screen shows every rep whether Slack is connected, because
    # "route this to Slack" is meaningless without knowing whether Slack exists yet.
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ChannelsOut:
    """Which channels this workspace has connected."""
    states = await connection_states(ts)
    return ChannelsOut(channels=[ChannelOut(**states[k]) for k in ALERT_CHANNEL_KINDS])


@router.put("/{kind}", response_model=ChannelOut)
async def connect(
    kind: str,
    body: ConnectIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> ChannelOut:
    """Connect (or replace) this workspace's credential for one channel.

    Every field the channel needs must be present. A half-filled credential stores a row that looks
    connected and delivers nothing — Telegram is the clear case, where a bot token without a chat
    id has a sender and no destination.
    """
    _require_kind(kind)
    needed = CHANNEL_FIELDS[kind]
    values = {f: (getattr(body, f, "") or "").strip() for f in needed}
    missing = [f for f, v in values.items() if not v]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{kind} needs {', '.join(needed)}; missing {', '.join(missing)}",
        )
    # A webhook URL that is not a URL fails at delivery time, inside a try/except that swallows —
    # so it would look connected and simply never arrive.
    if "url" in values and not values["url"].lower().startswith("https://"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the webhook URL must be an https:// address",
        )

    await save_connection(ts, kind=kind, secret=values, actor_user_id=principal.user_id)
    states = await connection_states(ts)
    return ChannelOut(**states[kind])


@router.post("/{kind}/test", response_model=ChannelOut)
async def test_connection(
    kind: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> ChannelOut:
    """Send a real message on this channel and record whether it arrived.

    A saved credential is not a working one, and "connected" that means "we stored a string" is the
    state this codebase keeps finding — a channel configured and delivering nothing, indistinguishable
    from a quiet week. So the ladder only advances on a real send, mirroring
    `sources/service.py`: only this function writes `status`, and no request body can set it.
    """
    _require_kind(kind)
    from nexus.alerts.channels import Alert as _A
    from nexus.alerts.connections import resolve_alert_channels
    from nexus.core.db import utcnow
    from nexus.integrations import connections as conn

    row = await conn.get_connection(ts, kind)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{kind} is not connected")

    registry = await resolve_alert_channels(ts)
    probe = _A(
        id="test", title="NEXUS test alert",
        body="If you can read this, your alerts are wired up correctly.",
        severity="info", account_id=None, source="connection_test", channel=kind, meta={},
    )
    try:
        result = await registry.get(kind).deliver(probe)
        ok, detail = bool(result.ok), (result.detail or "")
    except Exception as exc:  # a failing test must report, never 500
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    # REDACTED before it is stored or returned. A Slack webhook URL IS the credential, and httpx
    # puts the full URL into `raise_for_status` — so without this, one failed test wrote the sealed
    # secret back out in plaintext into `last_error`, into this response, and onto the Integrations
    # page, right beside the encrypted copy.
    detail = redact(detail)

    row.status = "connected" if ok else "error"
    row.verified_at = utcnow() if ok else None
    row.last_error = "" if ok else detail
    await ts.flush()

    states = await connection_states(ts)
    return ChannelOut(**states[kind])


@router.delete("/{kind}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def disconnect(
    kind: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> Response:
    """Disconnect, falling back to the deployment default.

    Never refused. During an incident "stop sending our alerts there" must not be blocked by a
    state machine — the same rule as deactivating a payment credential or a provider key.
    """
    _require_kind(kind)
    await clear_connection(ts, kind)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
