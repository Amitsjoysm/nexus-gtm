# nexus/api/routers/alert_connections.py
"""Connect a workspace's own Slack, Teams, Telegram or alert inbox, and say what each one receives.

Until this existed the channels were deployment env vars, so a customer could not connect theirs —
and an operator who set one to help a single customer would have routed every tenant's alerts into
it. See ``nexus/alerts/connections.py`` for why that is the CRM credential bug in a new place.

**Manager and up** (`manage_alert_channels`), decided with the product owner on 2026-09-10. A
workspace has ONE Slack per ``uq_integration_connection_tenant_kind``, so a rep replacing the webhook
would redirect every teammate's alerts; a team lead setting up the channel their team works from is
normal, and sending them to an admin was the friction that left channels unconnected. Choosing which
of your OWN alerts go there stays rep-level, in ``routers/notifications.py``.

This router owns the team half of routing: a **workspace rule** (``PUT /{kind}/rules``) says a
shared channel receives a category for everyone. Nobody's personal "off" or quiet hours mutes it, and
it is the only way an alert on an unowned account reaches a channel.

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
    replace_channel_rules,
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
    #: Alert categories a workspace rule sends here for everyone. Empty means no rule: the channel
    #: receives only what members route to it themselves.
    categories: list[str] = Field(default_factory=list)


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


class RulesIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: The whole set this channel should receive. It replaces what was saved, so unticking removes.
    categories: list[str] = Field(default_factory=list, max_length=50)


class RulesOut(BaseModel):
    kind: str
    categories: list[str] = Field(default_factory=list)


def _require_kind(kind: str) -> str:
    if kind not in ALERT_CHANNEL_KINDS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown alert channel: {kind}"
        )
    return kind


@router.get("", response_model=ChannelsOut)
async def list_connections(
    ts: TenantSession = Depends(get_tenant_session),
    # Rep-level READ: the notification screen shows every rep whether Slack is connected, and what
    # the team already sends there, because "route this to Slack" is meaningless without knowing
    # whether Slack exists yet or already receives it.
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ChannelsOut:
    """Which channels this workspace has connected, and what each receives by workspace rule."""
    states = await connection_states(ts)
    return ChannelsOut(channels=[ChannelOut(**states[k]) for k in ALERT_CHANNEL_KINDS])


@router.put("/{kind}", response_model=ChannelOut)
async def connect(
    kind: str,
    body: ConnectIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_alert_channels)),
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
    # so it would look connected and simply never arrive. It is also an SSRF primitive: the same
    # guard delivery enforces is run here so a bad URL is refused at the form rather than stored and
    # discovered when it fires. Delivery re-checks regardless (DNS can rebind after storage).
    if "url" in values:
        from nexus.alerts.url_guard import WebhookURLRejected, validate_webhook_url
        from nexus.core.config import get_settings

        try:
            validate_webhook_url(
                values["url"], allow_private=get_settings().alert_webhook_allow_private
            )
        except WebhookURLRejected as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    await save_connection(ts, kind=kind, secret=values, actor_user_id=principal.user_id)
    states = await connection_states(ts)
    return ChannelOut(**states[kind])


@router.post("/{kind}/test", response_model=ChannelOut)
async def test_connection(
    kind: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_alert_channels)),
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
    _: Principal = Depends(require(Permission.manage_alert_channels)),
) -> Response:
    """Disconnect, falling back to the deployment default.

    Never refused. During an incident "stop sending our alerts there" must not be blocked by a
    state machine — the same rule as deactivating a payment credential or a provider key. The
    channel's workspace rules are kept, so reconnecting does not silently start from nothing.
    """
    _require_kind(kind)
    await clear_connection(ts, kind)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{kind}/rules", response_model=RulesOut)
async def set_rules(
    kind: str,
    body: RulesIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_alert_channels)),
) -> RulesOut:
    """Set which alert categories this channel receives for the whole workspace.

    Allowed before the channel is connected, and kept when its credential is replaced or removed:
    a rule is a decision about the team's channel, not about one webhook, and making a manager
    re-tick every box after swapping a Slack webhook is how a channel quietly stops receiving.

    Every category must be one the alert rules can emit. A rule for an alert type that never fires is
    silence a manager believes is a setting — the same check ``PUT /notifications`` applies.
    """
    _require_kind(kind)
    from nexus.alerts.rules import ALERT_CATEGORIES

    unknown = sorted({c for c in body.categories if c not in ALERT_CATEGORIES})
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown alert type: {', '.join(unknown)}"
        )
    saved = await replace_channel_rules(
        ts, kind=kind, categories=body.categories, actor_user_id=principal.user_id
    )
    return RulesOut(kind=kind, categories=saved)
