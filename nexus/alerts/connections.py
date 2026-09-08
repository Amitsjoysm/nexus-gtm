# nexus/alerts/connections.py
"""Per-tenant alert channel credentials: which Slack, which Teams, whose inbox.

Alert channels were deployment-global env vars read through a process-wide singleton, and
``AlertService._deliver`` called ``get_alert_channels()`` with no idea which tenant's alert it was
holding. Nothing leaked because nothing was configured — but one
``NEXUS_ALERT_SLACK_WEBHOOK_URL`` set to help a single customer would have posted EVERY tenant's
account names, funding signals and next-best-actions into that one workspace.

That is the bug ``nexus/ingestion/crm_credentials.py`` was written to close, in the same shape:

    "handle_sync_crm_due_accounts resolved ONE connector and looped every tenant with it,
     pushing tenant A's accounts into whichever portal the deployment env named."

So this is the same fix, on the same table. ``IntegrationConnection`` is a generic per-tenant
credential store with a ``kind`` discriminator, and its own docstring argues against a fourth table:
every behaviour here — write-only secret, status ladder, per-tenant resolution with an env fallback
— is identical to CRM and SEP.

**Precedence**, mirroring ``resolve_crm_connector`` exactly:

    an explicitly installed registry (``set_alert_channels`` — the test seam)
      -> the tenant's stored connections
        -> ``get_alert_channels()``, built from the deployment env

That last step is what makes this additive: a deployment configured only by env behaves exactly as
it did before this existed. Pinned by test.

A webhook URL **is** the credential — anyone holding a Slack incoming-webhook URL can post to that
channel — so it is sealed at rest with the same envelope as a CRM token and appears in no response
model.
"""
from __future__ import annotations

import logging

from nexus.core.tenancy import TenantSession
from nexus.integrations import connections
from nexus.models.integration import ALERT_CHANNEL_KINDS, IntegrationConnection

logger = logging.getLogger("nexus.alerts.connections")

#: Which secret field each channel stores, and what the connect form must collect. Kept here rather
#: than in the router so the API, the resolver and the tests cannot disagree about what "connected"
#: means for a given channel.
CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
    "slack": ("url",),
    "teams": ("url",),
    # Telegram needs both halves: a bot token identifies the sender, a chat id names the
    # destination, and either alone delivers nothing.
    "telegram": ("bot_token", "chat_id"),
    # SMTP is the deployment's to run; what a WORKSPACE chooses is where its alerts land.
    "email": ("to",),
}


async def save_connection(
    ts: TenantSession,
    *,
    kind: str,
    secret: dict,
    actor_user_id: str | None = None,
) -> IntegrationConnection:
    """Store this tenant's credential for one channel. Upserts — one row per (tenant, kind)."""
    if kind not in ALERT_CHANNEL_KINDS:
        raise ValueError(f"{kind} is not an alert channel")
    return await connections.store_credentials(
        ts, kind=kind, provider=kind, secret=secret, actor_user_id=actor_user_id
    )


async def clear_connection(ts: TenantSession, kind: str) -> bool:
    """Disconnect, falling back to the deployment default. True when a row was removed."""
    return await connections.clear_credentials(ts, kind)


async def connection_states(ts: TenantSession) -> dict[str, dict]:
    """What this tenant has connected, for the UI. Never includes a secret.

    Reports ``connected`` for a row whose secret still DECRYPTS to the fields that channel needs,
    not merely for a row that exists. A credential that no longer decrypts — a rotated key, a
    corrupt envelope — is a channel that will silently stop delivering, and the screen has to say
    "reconnect" rather than showing a tick.
    """
    out: dict[str, dict] = {}
    for kind in ALERT_CHANNEL_KINDS:
        row = await connections.get_connection(ts, kind)
        fields = CHANNEL_FIELDS.get(kind, ())
        usable = connections.has_credentials(row, fields=fields) if row is not None else False
        out[kind] = {
            "kind": kind,
            "connected": usable,
            # A row that exists but cannot be decrypted is its own state: the customer thinks they
            # connected it, and only "needs reconnecting" explains why nothing arrives.
            "needs_reconnect": row is not None and not usable,
            "status": row.status if row is not None else "not_connected",
            "verified_at": row.verified_at.isoformat() if row is not None and row.verified_at else None,
            "last_error": (row.last_error if row is not None else None) or "",
            "fields": list(fields),
        }
    return out


async def resolve_alert_channels(ts: TenantSession):
    """The channel registry THIS tenant's alerts must be delivered through.

    Built fresh per resolution rather than cached. The CRM resolver caches because building a
    connector opens a client and holds recorded state; a channel is a URL and a `_post` callable,
    so a cache here would buy nothing and cost the one thing that matters — a channel the customer
    just reconnected must take effect on the next alert, not after a TTL.
    """
    from nexus.alerts.channels import (
        AlertChannelRegistry,
        EmailChannel,
        InAppChannel,
        SlackChannel,
        TeamsChannel,
        TelegramChannel,
        WebhookChannel,
        get_alert_channels,
    )
    from nexus.core.config import get_settings

    # The explicit override wins outright: it is how tests install a recording registry, and a
    # stored credential must not silently replace it.
    from nexus.alerts import channels as _channels_mod

    if _channels_mod._registry is not None and getattr(
        _channels_mod._registry, "_explicit", False
    ):
        return _channels_mod._registry

    s = get_settings()
    bundles: dict[str, dict] = {}
    for kind in ALERT_CHANNEL_KINDS:
        try:
            row = await connections.get_connection(ts, kind)
        except Exception:  # a credential lookup must never stop an alert being delivered
            logger.warning("alert connection lookup failed for %s", kind, exc_info=True)
            row = None
        bundle = connections.secret_bundle(row) if row is not None else {}
        if row is not None and not bundle:
            # A row we cannot honour — rotated key, corrupt envelope. Fall through to the
            # deployment default rather than delivering nowhere; `connection_states` reports the
            # row as needing reconnection so the customer can fix it.
            logger.warning(
                "[alerts] tenant %s has an unusable stored %s credential", ts.tenant_id, kind
            )
        bundles[kind] = bundle

    return AlertChannelRegistry(
        [
            InAppChannel(),
            # The generic webhook stays deployment-level: it is an integration an operator wires,
            # not an account a rep connects.
            WebhookChannel(s.alert_webhook_url),
            SlackChannel(bundles["slack"].get("url") or s.alert_slack_webhook_url),
            TeamsChannel(bundles["teams"].get("url") or s.alert_teams_webhook_url),
            EmailChannel(
                s.alert_email_sender,
                host=s.alert_smtp_host,
                port=s.alert_smtp_port,
                username=s.alert_smtp_username,
                password=s.alert_smtp_password,
                # The workspace chooses WHERE its alerts land; the deployment still owns the SMTP
                # server that sends them.
                to=bundles["email"].get("to") or s.alert_email_to,
            ),
            TelegramChannel(
                bundles["telegram"].get("bot_token") or s.alert_telegram_bot_token,
                bundles["telegram"].get("chat_id") or s.alert_telegram_chat_id,
            ),
        ]
    )
