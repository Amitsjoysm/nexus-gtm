# tests/test_alert_channel_connections.py
"""A tenant's alerts go to a tenant's own Slack, not the deployment's.

Alert channels were deployment-global env vars read through a process-wide singleton, and
`AlertService._deliver` called `get_alert_channels()` with no idea which tenant the alert belonged
to. Nothing leaked because nothing was configured — but the moment an operator set one
`NEXUS_ALERT_SLACK_WEBHOOK_URL` to help a single customer, EVERY tenant's account names, funding
signals and next-best-actions would post into that one Slack workspace.

That is the CRM bug in `nexus/ingestion/crm_credentials.py`, verbatim in shape:

    "handle_sync_crm_due_accounts resolved ONE connector and looped every tenant with it,
     pushing tenant A's accounts into whichever portal the deployment env named."

The fix is the same fix, and it reuses the same table. `IntegrationConnection` was written as a
generic per-tenant credential store with a `kind` discriminator precisely so the third integration
would not need a fourth table — so alert channels become new kinds rather than a new schema.

Precedence mirrors `resolve_crm_connector` exactly: an explicitly installed registry (the test
seam) beats a stored connection, which beats the deployment env. That last step is what keeps a
deployment configured only by env behaving exactly as it did before this existed.
"""
from __future__ import annotations


from tests.conftest import make_tenant, tenant_session


async def _connect(tenant_id: str, kind: str, url: str):
    from nexus.alerts.connections import save_connection

    async with tenant_session(tenant_id) as ts:
        row = await save_connection(ts, kind=kind, secret={"url": url})
        await ts.flush()
        return row


async def _resolve(tenant_id: str):
    from nexus.alerts.connections import resolve_alert_channels

    async with tenant_session(tenant_id) as ts:
        return await resolve_alert_channels(ts)


# ---- the leak ------------------------------------------------------------------------------------

async def test_two_tenants_get_their_own_slack(fresh_db):
    """THE property. One tenant's alerts must never post into another's workspace."""
    a = await make_tenant("ta", "Tenant A")
    b = await make_tenant("tb", "Tenant B")
    await _connect(a, "slack", "https://hooks.slack.com/services/AAA")
    await _connect(b, "slack", "https://hooks.slack.com/services/BBB")

    ra, rb = await _resolve(a), await _resolve(b)
    assert ra.get("slack")._url == "https://hooks.slack.com/services/AAA"
    assert rb.get("slack")._url == "https://hooks.slack.com/services/BBB"


async def test_a_tenant_with_no_connection_does_not_inherit_anothers(fresh_db, monkeypatch):
    """The dangerous direction. A workspace that has connected nothing must fall back to the
    DEPLOYMENT env — never to whatever another tenant happens to have saved."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "alert_slack_webhook_url", "")
    a = await make_tenant("ta", "Tenant A")
    b = await make_tenant("tb", "Tenant B")
    await _connect(a, "slack", "https://hooks.slack.com/services/AAA")

    assert (await _resolve(b)).get("slack")._url == "", "tenant B inherited tenant A's Slack"


async def test_the_env_is_the_fallback_not_the_answer(fresh_db, monkeypatch):
    """A deployment configured only by env behaves exactly as it did before this existed — the
    same property that made the CRM change safe to ship."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "alert_slack_webhook_url", "https://hooks.slack.com/ENV")
    t = await make_tenant()
    assert (await _resolve(t)).get("slack")._url == "https://hooks.slack.com/ENV"

    # And a stored connection OVERRIDES it, rather than being ignored.
    await _connect(t, "slack", "https://hooks.slack.com/services/OWN")
    assert (await _resolve(t)).get("slack")._url == "https://hooks.slack.com/services/OWN"


async def test_delivery_resolves_per_tenant(fresh_db):
    """Structural: `_deliver` must resolve against the alert's own tenant. Calling the global
    singleton is the bug — it is what had no idea whose alert it was holding."""
    import inspect

    from nexus.alerts.service import AlertService

    src = inspect.getsource(AlertService._deliver)
    assert "resolve_alert_channels" in src, "_deliver still uses the process-wide registry"


# ---- the secret ----------------------------------------------------------------------------------

async def test_the_webhook_url_is_sealed_at_rest(fresh_db):
    """A Slack webhook URL IS the credential — anyone holding it can post to that channel. Sealed
    with the same envelope as a CRM token, and never returned by any response model."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.integration import IntegrationConnection

    t = await make_tenant()
    await _connect(t, "slack", "https://hooks.slack.com/services/SECRET")

    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(IntegrationConnection))).one()
    blob = str(row.secret)
    assert "SECRET" not in blob, f"the webhook URL is stored in the clear: {blob[:120]}"
    assert "enc" in row.secret


async def test_the_api_never_returns_the_secret(fresh_db):
    """`_connection_out` in the CRM router is the single place connection state becomes JSON, and
    that is what makes "the secret never leaves the server" checkable. Same rule here."""

    from nexus.api.routers import alert_connections

    fields = set(alert_connections.ChannelOut.model_fields)
    for leak in ("secret", "url", "bot_token", "chat_id", "to"):
        assert leak not in fields, f"ChannelOut exposes {leak}"
    assert "connected" in fields and "needs_reconnect" in fields


# ---- the kinds -----------------------------------------------------------------------------------

def test_the_channel_kinds_reuse_the_integration_table():
    """A fourth credential table was the alternative. The model's own docstring argues against it:
    every behaviour — write-only secret, status ladder, per-tenant resolution with an env fallback —
    is identical, and a second table would duplicate the whole surface."""
    from nexus.models.integration import CONNECTION_KINDS

    for kind in ("slack", "teams", "telegram", "email"):
        assert kind in CONNECTION_KINDS, f"{kind} is not a connectable integration kind"
    # The originals must survive.
    assert "crm" in CONNECTION_KINDS and "sep" in CONNECTION_KINDS


async def test_one_connection_per_kind_per_tenant(fresh_db):
    """`uq_integration_connection_tenant_kind` already enforces it. Connecting twice must UPDATE,
    or a tenant ends up with two Slack rows and delivery becomes a coin flip."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.integration import IntegrationConnection

    t = await make_tenant()
    await _connect(t, "slack", "https://hooks.slack.com/services/FIRST")
    await _connect(t, "slack", "https://hooks.slack.com/services/SECOND")

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(
            select(IntegrationConnection).where(IntegrationConnection.kind == "slack")
        )).all()
    assert len(rows) == 1
    assert (await _resolve(t)).get("slack")._url.endswith("SECOND")


async def test_a_tenant_can_hold_several_channels_at_once(fresh_db):
    """The unique constraint is on (tenant, kind), so Slack and Teams coexist — which is the whole
    point of a per-kind row rather than one 'alerting' credential."""
    t = await make_tenant()
    await _connect(t, "slack", "https://hooks.slack.com/services/S")
    await _connect(t, "teams", "https://outlook.office.com/webhook/T")

    reg = await _resolve(t)
    assert reg.get("slack")._url.endswith("/S")
    assert reg.get("teams")._url.endswith("/T")


async def test_an_unusable_stored_secret_falls_back_rather_than_failing(fresh_db, monkeypatch):
    """Same posture as the CRM resolver: a row we cannot honour (key rotated, secret corrupt) must
    not take alert delivery down. It degrades to the deployment default and the connection screen
    reports the row as needing reconnection."""
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.integration import IntegrationConnection

    monkeypatch.setattr(get_settings(), "alert_slack_webhook_url", "https://hooks.slack.com/ENV")
    t = await make_tenant()
    await _connect(t, "slack", "https://hooks.slack.com/services/OWN")

    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(IntegrationConnection))).one()
        row.secret = {"enc": "not-a-valid-envelope"}
        await s.commit()

    assert (await _resolve(t)).get("slack")._url == "https://hooks.slack.com/ENV"
