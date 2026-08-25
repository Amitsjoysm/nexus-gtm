# nexus/integrations/sep_credentials.py
"""Per-tenant SEP credentials: connector resolution over the shared connection store.

The exact mirror of :mod:`nexus.ingestion.crm_credentials`, and deliberately so — the two
subsystems have the same shape and diverging their precedence rules would be a source of bugs
nobody could reason about. Storage is shared (:mod:`nexus.integrations.connections`, ``kind="sep"``);
this module owns only which connector a stored bundle builds.

Precedence:

  1. a deliberately installed connector (``set_sep_connector`` — the test seam) wins;
  2. else the tenant's stored credential;
  3. else the deployment default, which is the explicit stub.

Step 3 is why a deployment that never configures SEP keeps behaving as before: pushes are
recorded, not attempted. What changed is that the recording is now *named* a stub rather than
masquerading as Outreach.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from nexus.core.tenancy import TenantSession
from nexus.integrations import connections
from nexus.integrations.sep import (
    OutreachConnector,
    SalesloftConnector,
    SEPConnector,
    get_sep_connector,
    get_sep_connector_override,
)
from nexus.models.integration import IntegrationConnection

logger = logging.getLogger("nexus.integrations.sep")

KIND = "sep"

KNOWN_SEP_PROVIDERS = ("salesloft", "outreach")
LIVE_SEP_PROVIDERS = ("salesloft", "outreach")

# Salesloft is an API key; Outreach is OAuth. Either makes a stored credential usable.
CREDENTIAL_FIELDS = ("api_key", "access_token", "refresh_token")

# Outreach has no API-key path at all, so a pasted key cannot connect it. Saying so is better
# than storing something that will fail on first use with an opaque 401.
OAUTH_ONLY_SEP_PROVIDERS = ("outreach",)

_CACHE_MAX = 128
_TENANT_CONNECTORS: "OrderedDict[str, tuple[str, SEPConnector]]" = OrderedDict()


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
    return connections.has_credentials(row, fields=CREDENTIAL_FIELDS)


async def get_connection(ts: TenantSession) -> IntegrationConnection | None:
    return await connections.get_connection(ts, KIND)


def build_tenant_connector(
    provider: str, bundle: dict, *, token_provider=None
) -> SEPConnector | None:
    """Build a connector from a decrypted bundle, or ``None`` if we cannot honor it."""
    if provider == "salesloft":
        key = str(bundle.get("api_key") or bundle.get("access_token") or "")
        return SalesloftConnector(token=key) if key else None
    if provider == "outreach":
        token = str(bundle.get("access_token") or "")
        if token or bundle.get("refresh_token"):
            return OutreachConnector(token=token, token_provider=token_provider)
    return None


def _make_token_provider(ts: TenantSession, row: IntegrationConnection):
    """Refresh this tenant's OAuth access token and persist it. ``None`` when not refreshable."""
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


async def resolve_sep_connector(ts: TenantSession) -> SEPConnector:
    """The SEP connector this tenant's pushes must use."""
    override = get_sep_connector_override()
    if override is not None:
        return override

    row = await get_connection(ts)
    if row is not None:
        fingerprint = _fingerprint(row)
        cached = _TENANT_CONNECTORS.get(ts.tenant_id)
        if cached is not None and cached[0] == fingerprint:
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            return cached[1]

        connector = build_tenant_connector(
            row.provider, connections.secret_bundle(row),
            token_provider=_make_token_provider(ts, row),
        )
        if connector is not None:
            _TENANT_CONNECTORS[ts.tenant_id] = (fingerprint, connector)
            _TENANT_CONNECTORS.move_to_end(ts.tenant_id)
            while len(_TENANT_CONNECTORS) > _CACHE_MAX:
                _TENANT_CONNECTORS.popitem(last=False)
            return connector

        logger.warning(
            "[sep] tenant %s has an unusable stored credential (provider=%s)",
            ts.tenant_id, row.provider,
        )
        invalidate_tenant_connector(ts.tenant_id)

    return get_sep_connector()


async def store_credentials(
    ts: TenantSession,
    *,
    provider: str,
    api_key: str | None = None,
    actor_user_id: str | None = None,
    bundle: dict | None = None,
) -> IntegrationConnection:
    """Upsert the tenant's SEP connection. ``api_key``/``bundle`` are write-only."""
    secret = bundle if bundle else ({"api_key": api_key} if api_key else None)
    row = await connections.store_credentials(
        ts, kind=KIND, provider=provider, secret=secret, actor_user_id=actor_user_id,
    )
    invalidate_tenant_connector(ts.tenant_id)
    return row


async def clear_credentials(ts: TenantSession) -> bool:
    """Delete the tenant's SEP connection so it falls back to the default."""
    removed = await connections.clear_credentials(ts, KIND)
    invalidate_tenant_connector(ts.tenant_id)
    return removed
