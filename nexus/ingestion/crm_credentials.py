# nexus/ingestion/crm_credentials.py
"""Per-tenant CRM credentials: connector resolution over the shared connection store.

Storage lives in :mod:`nexus.integrations.connections` (shared with SEP); this module owns the
CRM-specific half — which connector a stored credential builds, and the precedence that decides
whether a stored credential is used at all.

Resolution is layered so a deployment that only sets ``NEXUS_CRM_PROVIDER`` /
``NEXUS_HUBSPOT_ACCESS_TOKEN`` keeps behaving exactly as it did before per-tenant credentials
existed:

  1. a deliberately installed connector (``set_crm_connector`` — the test seam) wins;
  2. else the tenant's stored credential;
  3. else the env-configured connector.

Every request and worker path should call :func:`resolve_crm_connector` rather than
``get_crm_connector()``. Resolving once for a whole process — as the heartbeat sweep used to —
pushes every tenant's accounts into whichever CRM the deployment env points at.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import (
    CRMConnector,
    HubSpotConnector,
    SalesforceConnector,
    get_crm_connector,
    get_crm_connector_override,
)
from nexus.integrations import connections
from nexus.models.integration import IntegrationConnection

logger = logging.getLogger("nexus.integrations.crm")

KIND = "crm"

# Provider names the product recognises, and the subset with a working API client.
KNOWN_CRM_PROVIDERS = ("hubspot", "salesforce")
LIVE_CRM_PROVIDERS = ("hubspot", "salesforce")

# Any one of these makes a stored credential usable. A workspace that connected via OAuth has no
# access_token the admin typed but does have a refresh_token, and must not read as unconfigured.
CREDENTIAL_FIELDS = ("access_token", "refresh_token")

_CACHE_MAX = 128
# tenant_id -> (fingerprint, connector).
#
# What this cache does: keeps the connector *instance* stable, so the per-instance recording
# buffers capped by CRMConnector.MAX_RECORDED_PUSHES survive across pushes instead of resetting
# on every call, and skips a decrypt + construction each time.
#
# What it does NOT do: skip the row read. Resolution reads the row every call on purpose — that
# single indexed lookup is how a worker process notices a credential the API process just
# changed. N+1 query pressure is handled by hoisting resolution out of inner loops, not here.
_TENANT_CONNECTORS: "OrderedDict[str, tuple[str, CRMConnector]]" = OrderedDict()


def invalidate_tenant_connector(tenant_id: str | None = None) -> None:
    """Drop cached connectors. ``None`` clears every tenant."""
    if tenant_id is None:
        _TENANT_CONNECTORS.clear()
    else:
        _TENANT_CONNECTORS.pop(tenant_id, None)


def _fingerprint(row: IntegrationConnection) -> str:
    stamp = row.updated_at.isoformat() if row.updated_at else ""
    return f"{stamp}|{row.provider}|{row.api_base}"


def has_credentials(row: IntegrationConnection | None) -> bool:
    """True when the row holds a secret that still decrypts to a usable credential."""
    return connections.has_credentials(row, fields=CREDENTIAL_FIELDS)


async def get_connection(ts: TenantSession) -> IntegrationConnection | None:
    """The tenant's stored CRM connection row, if any."""
    return await connections.get_connection(ts, KIND)


def build_tenant_connector(
    provider: str, bundle: dict, api_base: str = "", *, token_provider=None
) -> CRMConnector | None:
    """Build a connector from a decrypted bundle, or ``None`` if we cannot honor it.

    ``token_provider`` is the async callback an OAuth-backed connector uses to refresh an expired
    access token; a pasted private-app token needs none, so it stays optional.
    """
    token = str(bundle.get("access_token") or "")
    if provider == "hubspot" and (token or bundle.get("refresh_token")):
        kwargs = {"access_token": token, "token_provider": token_provider}
        if api_base:
            kwargs["api_base"] = api_base
        return HubSpotConnector(**kwargs)
    if provider == "salesforce" and (token or bundle.get("refresh_token")):
        instance_url = str(bundle.get("instance_url") or api_base or "")
        if not instance_url:
            # Salesforce REST is addressed at the org's own host, which the token response
            # carries. Without it there is nowhere to send the request.
            return None
        return SalesforceConnector(
            access_token=token, instance_url=instance_url, token_provider=token_provider
        )
    return None


async def resolve_crm_connector(ts: TenantSession) -> CRMConnector:
    """The connector this tenant's syncs must use. See the module docstring for precedence."""
    override = get_crm_connector_override()
    if override is not None:
        return override

    row = await get_connection(ts)
    if row is not None:
        fingerprint = _fingerprint(row)
        cached = _TENANT_CONNECTORS.get(ts.tenant_id)
        if cached is not None and cached[0] == fingerprint:
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            return cached[1]

        bundle = connections.secret_bundle(row)
        connector = build_tenant_connector(
            row.provider, bundle, row.api_base or "",
            token_provider=_make_token_provider(ts, row),
        )
        if connector is not None:
            _TENANT_CONNECTORS[ts.tenant_id] = (fingerprint, connector)
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            while len(_TENANT_CONNECTORS) > _CACHE_MAX:
                _TENANT_CONNECTORS.popitem(last=False)
            return connector

        # A row we cannot honor: an unknown provider, or a secret that no longer decrypts because
        # the key rotated. Fall through to env rather than fail the sync; the connection endpoint
        # reports the row as needing reconnection.
        logger.warning(
            "[crm] tenant %s has an unusable stored credential (provider=%s)",
            ts.tenant_id, row.provider,
        )
        invalidate_tenant_connector(ts.tenant_id)

    return get_crm_connector()


def _make_token_provider(ts: TenantSession, row: IntegrationConnection):
    """An async callback that refreshes this tenant's OAuth access token and persists it.

    Returns ``None`` for a credential with no refresh token — a pasted private-app token cannot be
    refreshed, and handing the connector a provider that always fails would turn a clear
    "invalid token" into a confusing refresh error.
    """
    if not connections.secret_bundle(row).get("refresh_token"):
        return None

    async def _refresh() -> str:
        from nexus.integrations.oauth import refresh_access_token

        bundle = connections.secret_bundle(row)
        updated = await refresh_access_token(row.provider, bundle)
        if not updated:
            return ""
        await connections.merge_secret(ts, row, updated)
        invalidate_tenant_connector(ts.tenant_id)
        return str(updated.get("access_token") or "")

    return _refresh


async def store_credentials(
    ts: TenantSession,
    *,
    provider: str,
    access_token: str | None,
    api_base: str = "",
    actor_user_id: str | None = None,
    bundle: dict | None = None,
) -> IntegrationConnection:
    """Upsert the tenant's CRM connection.

    ``access_token`` is write-only: a blank or omitted value keeps the stored secret, so an admin
    can change the provider or api_base without re-entering the token. ``bundle`` is the OAuth
    path, carrying the full token set at once.
    """
    secret = bundle if bundle else ({"access_token": access_token} if access_token else None)
    row = await connections.store_credentials(
        ts, kind=KIND, provider=provider, secret=secret,
        api_base=api_base, actor_user_id=actor_user_id,
    )
    invalidate_tenant_connector(ts.tenant_id)
    return row


async def clear_credentials(ts: TenantSession) -> bool:
    """Delete the tenant's CRM connection so it falls back to env. True when a row was removed."""
    removed = await connections.clear_credentials(ts, KIND)
    invalidate_tenant_connector(ts.tenant_id)
    return removed
