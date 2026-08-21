# nexus/integrations/connections.py
"""Per-tenant integration credentials: the storage half, shared by every integration kind.

CRM and SEP need exactly the same things — load the tenant's row, upsert it with a write-only
secret, clear it, and report whether the stored secret still decrypts. Only the *connector* built
from those credentials differs, so that is the part each subsystem owns
(``ingestion/crm_credentials.py``, ``integrations/sep_credentials.py``) and this is the part they
share.

Everything here is keyed by ``kind`` so one tenant can hold a CRM and a SEP credential at once
without either resolver ever seeing the other's row — a CRM resolver handed a Salesloft API key
would build a connector that fails in a way nobody could diagnose from the error.
"""
from __future__ import annotations

from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from nexus.models.integration import IntegrationConnection


async def get_connection(ts: TenantSession, kind: str) -> IntegrationConnection | None:
    """The tenant's stored connection of this kind, if any. One row per (tenant, kind)."""
    return await ts.first(IntegrationConnection, IntegrationConnection.kind == kind)


def secret_bundle(row: IntegrationConnection | None) -> dict:
    """The decrypted credential bundle, or ``{}`` when absent or no longer decryptable."""
    return unseal_crm_secret(row.secret) if row is not None else {}


def has_credentials(row: IntegrationConnection | None, *, fields: tuple[str, ...]) -> bool:
    """True when the row holds a secret that still decrypts to at least one of ``fields``.

    ``fields`` rather than a single key because a credential can arrive by more than one route:
    a HubSpot connection is usable with a pasted ``access_token`` *or* with an OAuth
    ``refresh_token``, and a workspace that connected via OAuth has no token the admin typed. A
    single-key check would report such a workspace as unconfigured.
    """
    bundle = secret_bundle(row)
    return any(bundle.get(f) for f in fields)


async def store_credentials(
    ts: TenantSession,
    *,
    kind: str,
    provider: str,
    secret: dict | None,
    api_base: str = "",
    actor_user_id: str | None = None,
) -> IntegrationConnection:
    """Upsert the tenant's connection of this kind.

    ``secret`` is write-only: ``None`` (or empty) keeps whatever is stored, so an admin can change
    the provider or api_base without re-entering a token. Any save resets the row to
    ``unverified`` — a credential is not "connected" until a test says so.
    """
    row = await get_connection(ts, kind)
    if row is None:
        row = IntegrationConnection(tenant_id=ts.tenant_id, kind=kind, provider=provider, secret={})
        ts.add(row)
    row.provider = provider
    row.api_base = api_base
    if secret:
        row.secret = seal_crm_secret(secret)
    row.status = "unverified"
    row.verified_at = None
    row.last_error = None
    row.updated_by_user_id = actor_user_id
    await ts.flush()
    return row


async def merge_secret(ts: TenantSession, row: IntegrationConnection, updates: dict) -> None:
    """Merge fields into the stored bundle, keeping the rest.

    This is how a refreshed OAuth access token is persisted without losing the refresh token that
    obtained it.
    """
    bundle = secret_bundle(row)
    bundle.update(updates)
    row.secret = seal_crm_secret(bundle)
    await ts.flush()


async def clear_credentials(ts: TenantSession, kind: str) -> bool:
    """Delete the tenant's connection of this kind so it falls back to env. True when removed."""
    row = await get_connection(ts, kind)
    if row is None:
        return False
    await ts.delete(row)
    await ts.flush()
    return True
