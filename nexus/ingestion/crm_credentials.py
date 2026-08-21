# nexus/ingestion/crm_credentials.py
"""Per-tenant CRM credentials: encrypted storage + connector resolution.

A tenant connects its own CRM by storing an access token here. The token is sealed
(:mod:`nexus.ingestion.crm_crypto`) and never leaves the server.

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
    get_crm_connector,
    get_crm_connector_override,
)
from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from nexus.models.integration import CrmConnection

logger = logging.getLogger("nexus.integrations.crm")

# Provider names the product recognises, and the subset with a working API client. Salesforce is
# known (so the UI can render it, disabled, and the model can store it later) but not live:
# SalesforceConnector.fetch_accounts returns an injected sample, so accepting a Salesforce token
# would mean storing a secret that does nothing.
KNOWN_CRM_PROVIDERS = ("hubspot", "salesforce")
LIVE_CRM_PROVIDERS = ("hubspot",)

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


def _fingerprint(row: CrmConnection) -> str:
    stamp = row.updated_at.isoformat() if row.updated_at else ""
    return f"{stamp}|{row.provider}|{row.api_base}"


def has_credentials(row: CrmConnection | None) -> bool:
    """True when the row holds a secret that still decrypts to a usable token."""
    return bool(row and unseal_crm_secret(row.secret).get("access_token"))


def build_tenant_connector(provider: str, token: str, api_base: str = "") -> CRMConnector | None:
    """Build a connector from decrypted credentials, or ``None`` if we cannot honor them."""
    if provider == "hubspot" and token:
        if api_base:
            return HubSpotConnector(access_token=token, api_base=api_base)
        return HubSpotConnector(access_token=token)
    return None


async def get_connection(ts: TenantSession) -> CrmConnection | None:
    """The tenant's stored CRM connection row, if any. One row per tenant."""
    return await ts.first(CrmConnection)


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

        token = str(unseal_crm_secret(row.secret).get("access_token") or "")
        connector = build_tenant_connector(row.provider, token, row.api_base or "")
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


async def store_credentials(
    ts: TenantSession,
    *,
    provider: str,
    access_token: str | None,
    api_base: str = "",
    actor_user_id: str | None = None,
) -> CrmConnection:
    """Upsert the tenant's CRM connection.

    ``access_token`` is write-only: a blank or omitted value keeps the stored secret, so an admin
    can change the provider or api_base without re-entering the token. Any save resets the row to
    ``unverified`` — a credential is not "connected" until a test says so.
    """
    row = await get_connection(ts)
    if row is None:
        row = CrmConnection(tenant_id=ts.tenant_id, provider=provider, secret={})
        ts.add(row)
    row.provider = provider
    row.api_base = api_base
    if access_token:
        row.secret = seal_crm_secret({"access_token": access_token})
    row.status = "unverified"
    row.verified_at = None
    row.last_error = None
    row.updated_by_user_id = actor_user_id
    await ts.flush()
    invalidate_tenant_connector(ts.tenant_id)
    return row


async def clear_credentials(ts: TenantSession) -> bool:
    """Delete the tenant's connection so it falls back to env. True when a row was removed."""
    row = await get_connection(ts)
    if row is None:
        return False
    await ts.delete(row)
    await ts.flush()
    invalidate_tenant_connector(ts.tenant_id)
    return True
