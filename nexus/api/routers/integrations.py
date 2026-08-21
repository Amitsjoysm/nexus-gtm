"""Integration endpoints: CRM sync/push (Salesforce/HubSpot) and SEP push (Outreach/Salesloft).

CRM connectors are bi-directional. Inbound (``/crm/sync``) pulls 1st-party accounts and upserts
them by ``(tenant, crm_source, crm_id)``; the endpoint accepts records inline so the integration
is exercisable offline and in tests. Outbound (``/crm/push/{account_id}``) writes a NEXUS-enriched
account and its contacts back to the configured CRM. Real deployments swap the stub's body for an
authenticated API call without changing either surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import case, func, or_, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    CRMConnectionIn,
    CRMConnectionOut,
    CRMConnectionTestOut,
    CRMPushResponse,
    CRMSyncRequest,
    CRMSyncResponse,
    CRMSyncStatusOut,
    SEPConnectionIn,
    SEPConnectionOut,
    SEPPushRequest,
    SEPPushResponse,
)
from nexus.core.audit import record_audit
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import (
    CRMAccount,
    CRMConnector,
)
from nexus.integrations import connections
from nexus.ingestion.crm_credentials import (
    KNOWN_CRM_PROVIDERS,
    LIVE_CRM_PROVIDERS,
    clear_credentials,
    get_connection,
    has_credentials,
    resolve_crm_connector,
    store_credentials,
)
from nexus.ingestion.crm_sync import sync_account_to_crm
from nexus.integrations.sep_credentials import (
    KNOWN_SEP_PROVIDERS,
    OAUTH_ONLY_SEP_PROVIDERS,
    clear_credentials as sep_clear_credentials,
    get_connection as sep_get_connection,
    has_credentials as sep_has_credentials,
    resolve_sep_connector,
    store_credentials as sep_store_credentials,
)
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant
from nexus.models.integration import IntegrationConnection

router = APIRouter(prefix="/integrations", tags=["integrations"])

CRM_SOURCES = ("salesforce", "hubspot")


def _env_provider() -> str:
    """The deployment-wide CRM provider — the fallback when a tenant has no connection."""
    return (get_settings().crm_provider or "stub").strip().lower()


class _PostedRows(CRMConnector):
    """The rows the caller posted, tagged with the source they named.

    This used to be done by looking the source up in a dict of connector CLASSES and calling it
    with ``sample=`` — which only worked because the Salesforce connector is still a stub whose
    constructor happens to take that keyword. `HubSpotConnector.__init__` takes an access token,
    so choosing "HubSpot" in the Integrations screen raised
    ``TypeError: got an unexpected keyword argument 'sample'`` and the user saw a 500. Measured
    against the running stack before the fix: salesforce 200, hubspot 500.

    Carrying the source as data rather than as a class removes the coupling entirely: what the
    caller posts is imported the same way whichever CRM they say it came from, which is all this
    path ever did.
    """

    def __init__(self, source: str, sample: list[CRMAccount]):
        super().__init__()
        self.source = source
        self._sample = sample

    async def fetch_accounts(self) -> list[CRMAccount]:
        return self._sample


@router.post("/crm/sync", response_model=CRMSyncResponse)
async def crm_sync(
    body: CRMSyncRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CRMSyncResponse:
    """Import accounts into NEXUS under a CRM source.

    Two paths, and which one runs depends on whether the caller posted rows:

    * **Rows posted** — they are imported as-is, tagged with ``source``. This is what the
      Integrations screen drives today: a hand-entry grid, not a connection.
    * **No rows** — pull from the CRM that this deployment is actually configured for
      (``NEXUS_CRM_PROVIDER`` + its credentials). Asking to pull from a provider the deployment
      is not wired to is a 400 rather than an empty success: "nothing came back" and "we were
      never connected to that CRM" are different facts and must not look the same.
    """
    # `source` is already constrained by the request schema's pattern; `CRM_SOURCES` is the same
    # set as a value the rest of the code can read, and a test asserts the two stay in step.
    sample = [
        CRMAccount(
            external_id=a.external_id,
            name=a.name,
            domain=a.domain,
            industry=a.industry,
            employee_count=a.employee_count,
            country=a.country,
        )
        for a in body.accounts
    ]

    if sample:
        connector: CRMConnector = _PostedRows(body.source, sample)
    else:
        # Pull from the CRM this *tenant* is connected to, falling back to the deployment's.
        connector = await resolve_crm_connector(ts)
        if connector.source != body.source:
            connection = await get_connection(ts)
            where = (
                f"this workspace is connected to '{connection.provider}'"
                if has_credentials(connection)
                else f"NEXUS_CRM_PROVIDER is '{_env_provider()}'"
            )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Not connected to {body.source} ({where}), so there is nothing to pull. "
                f"Post accounts to import them manually, or connect {body.source} "
                f"in Integrations.",
            )

    accounts = await connector.sync_accounts(ts)
    return CRMSyncResponse(
        source=body.source,
        synced=len(accounts),
        account_ids=[a.id for a in accounts],
    )


@router.post("/crm/push/{account_id}", response_model=CRMPushResponse)
async def crm_push(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CRMPushResponse:
    """Write a NEXUS-enriched account (+ its contacts) back to the outbound CRM."""
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    contacts = await ts.list(Contact, Contact.account_id == account_id)
    # Route through the shared sync unit so a manual push stamps crm_synced_at exactly like
    # auto-sync does — the trust chip ("Synced to Salesforce · 2m ago") must reflect manual
    # pushes too, and change-detection must not re-push an account a rep just pushed.
    result = await sync_account_to_crm(
        ts, account, connector=await resolve_crm_connector(ts), now=utcnow()
    )
    return CRMPushResponse(
        ok=result.ok,
        source=result.source,
        external_id=result.external_id,
        contacts=len(contacts),
    )


@router.get("/crm/sync-status", response_model=CRMSyncStatusOut)
async def crm_sync_status(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMSyncStatusOut:
    """Auto-sync state for the current tenant: whether it is active, the provider, and how many
    accounts are pending vs. up to date. Counts are tenant-scoped (RLS) — never a global scan."""
    settings = get_settings()
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    enabled = bool(settings.crm_sync_enabled and tenant and tenant.automation_enabled)

    # The tenant's own CRM when it has one — reporting the deployment-wide provider here told a
    # connected tenant the wrong thing.
    connection = await get_connection(ts)
    tenant_provider = connection.provider if has_credentials(connection) else _env_provider()

    due_where = or_(
        Account.crm_synced_at.is_(None),
        Account.updated_at > Account.crm_synced_at,
    )
    # total + pending in one scan via conditional aggregation (one round trip, not two).
    row = (
        await ts.session.execute(
            select(
                func.count().label("total"),
                func.sum(case((due_where, 1), else_=0)).label("pending"),
            )
            .select_from(Account)
            .where(Account.tenant_id == ts.tenant_id)
        )
    ).one()
    total = int(row.total or 0)
    pending = int(row.pending or 0)
    return CRMSyncStatusOut(
        enabled=enabled,
        provider=tenant_provider,
        pending=pending,
        synced=total - pending,
    )


# ---- per-tenant CRM connection -------------------------------------------------------
# A tenant connects its own CRM here. Before this existed every tenant shared one
# deployment-wide token, so a customer could not connect their own CRM and the heartbeat
# sweep pushed every tenant's accounts to whichever portal the env pointed at.


def _connection_out(row: IntegrationConnection | None, *, env_provider: str) -> CRMConnectionOut:
    """Project a stored row (or the env fallback) into the response model.

    The only place connection state becomes JSON — keeping it in one function is what makes
    "the secret never leaves the server" checkable by reading a single body.
    """
    if row is not None:
        stored = has_credentials(row)
        return CRMConnectionOut(
            provider=row.provider,
            source="tenant",
            has_credentials=stored,
            # A row whose secret no longer decrypts (key rotation) is an error the admin must
            # see and act on — not silently reported as "no connection".
            status=row.status if stored else "error",
            api_base=row.api_base or "",
            verified_at=row.verified_at.isoformat() if (stored and row.verified_at) else None,
            last_error=(
                row.last_error
                if stored
                else "Stored credential could not be decrypted — reconnect your CRM."
            ),
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    if env_provider and env_provider != "stub":
        # Configured deployment-wide. Never tested from here, so it is not claimed as verified.
        return CRMConnectionOut(provider=env_provider, source="env", status="unverified")
    return CRMConnectionOut(provider=env_provider or "stub", source="none", status="none")


@router.get("/crm/connection", response_model=CRMConnectionOut)
async def get_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionOut:
    """The tenant's CRM connection state. Never includes the credential."""
    return _connection_out(await get_connection(ts), env_provider=_env_provider())


@router.put("/crm/connection", response_model=CRMConnectionOut)
async def set_crm_connection(
    body: CRMConnectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionOut:
    """Store (or update) this tenant's CRM credentials."""
    provider = (body.provider or "").strip().lower()
    if provider not in KNOWN_CRM_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown CRM provider '{provider}'")
    if provider not in LIVE_CRM_PROVIDERS:
        # Storing a credential we cannot actually use would be a silent no-op for the customer.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider.capitalize()} connections are not available yet.",
        )

    existing = await get_connection(ts)
    token = (body.access_token or "").strip()
    if not token and not has_credentials(existing):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An access token is required to connect a CRM."
        )

    api_base = (body.api_base or "").strip()
    if provider == "salesforce":
        # Salesforce REST is addressed at the org's own host, which OAuth returns as instance_url.
        # On the manual path the admin supplies it; without it the credential has nowhere to go,
        # so refuse rather than store something that can never work.
        known_host = api_base or str(
            connections.secret_bundle(existing).get("instance_url") or ""
        )
        if not known_host:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Salesforce needs your instance URL (e.g. https://acme.my.salesforce.com). "
                "Connect with OAuth to have it filled in automatically.",
            )

    row = await store_credentials(
        ts,
        provider=provider,
        access_token=token or None,
        api_base=api_base,
        actor_user_id=principal.user_id,
    )
    await record_audit(
        ts,
        "crm.connection.set",
        actor_user_id=principal.user_id,
        target_type="crm_connection",
        target_id=row.id,
        meta={"provider": provider, "token_set": bool(token), "api_base": row.api_base},
    )
    return _connection_out(row, env_provider=_env_provider())


@router.post("/crm/connection/test", response_model=CRMConnectionTestOut)
async def test_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionTestOut:
    """Verify the resolved connector's credentials and record the outcome on the row."""
    connector = await resolve_crm_connector(ts)
    result = await connector.test_connection()

    row = await get_connection(ts)
    if row is not None:
        if result.ok:
            row.status = "connected"
            row.verified_at = utcnow()
            row.last_error = None
        else:
            row.status = "error"
            row.last_error = (result.detail or "Connection test failed.")[:500]
        await ts.flush()

    await record_audit(
        ts,
        "crm.connection.test",
        actor_user_id=principal.user_id,
        target_type="crm_connection",
        target_id=row.id if row is not None else "",
        meta={"provider": connector.source, "ok": result.ok, "detail": result.detail},
    )
    return CRMConnectionTestOut(ok=result.ok, label=result.label, detail=result.detail)


@router.delete("/crm/connection", status_code=status.HTTP_204_NO_CONTENT)
async def clear_crm_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> Response:
    """Disconnect the tenant's CRM. Resolution falls back to the deployment env configuration."""
    removed = await clear_credentials(ts)
    await record_audit(
        ts,
        "crm.connection.clear",
        actor_user_id=principal.user_id,
        target_type="crm_connection",
        meta={"removed": removed},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sep/push", response_model=SEPPushResponse)
async def sep_push(
    body: SEPPushRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> SEPPushResponse:
    email = body.email
    if body.contact_id:
        contact = await ts.get(Contact, body.contact_id)
        if contact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
        email = email or contact.email
    connector = await resolve_sep_connector(ts)
    result = await connector.push_contact(
        sequence=body.sequence, email=email, payload=body.payload
    )
    return SEPPushResponse(ok=result.ok, platform=result.platform, detail=result.detail)


# ---- per-tenant SEP connection -------------------------------------------------------
def _sep_connection_out(row: IntegrationConnection | None) -> SEPConnectionOut:
    """The only place SEP connection state becomes JSON. The secret is never on it."""
    if row is not None:
        stored = sep_has_credentials(row)
        return SEPConnectionOut(
            provider=row.provider,
            source="tenant",
            has_credentials=stored,
            status=row.status if stored else "error",
            verified_at=row.verified_at.isoformat() if (stored and row.verified_at) else None,
            last_error=(
                row.last_error
                if stored
                else "Stored credential could not be decrypted — reconnect your SEP."
            ),
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    # No tenant credential: pushes are recorded by the stub, not delivered. Reporting that
    # honestly is the whole point of naming the stub.
    return SEPConnectionOut(provider="stub", source="default", status="none")


@router.get("/sep/connection", response_model=SEPConnectionOut)
async def get_sep_connection(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> SEPConnectionOut:
    """The tenant's SEP connection state. Never includes the credential."""
    return _sep_connection_out(await sep_get_connection(ts))


@router.put("/sep/connection", response_model=SEPConnectionOut)
async def set_sep_connection(
    body: SEPConnectionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> SEPConnectionOut:
    """Store (or update) this tenant's SEP credentials."""
    provider = (body.provider or "").strip().lower()
    if provider not in KNOWN_SEP_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown SEP provider '{provider}'")
    if provider in OAUTH_ONLY_SEP_PROVIDERS:
        # Outreach has no API-key path; a pasted key would 401 on first use with nothing to
        # explain why.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{provider.capitalize()} connects with OAuth, not an API key. "
            f"Use Connect {provider.capitalize()}.",
        )

    key = (body.api_key or "").strip()
    if not key and not sep_has_credentials(await sep_get_connection(ts)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An API key is required to connect a sequence platform."
        )

    row = await sep_store_credentials(
        ts, provider=provider, api_key=key or None, actor_user_id=principal.user_id
    )
    await record_audit(
        ts, "sep.connection.set", actor_user_id=principal.user_id,
        target_type="sep_connection", target_id=row.id,
        meta={"provider": provider, "key_set": bool(key)},
    )
    return _sep_connection_out(row)


@router.post("/sep/connection/test", response_model=CRMConnectionTestOut)
async def test_sep_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> CRMConnectionTestOut:
    """Verify the resolved SEP connector and record the outcome on the row."""
    connector = await resolve_sep_connector(ts)
    result = await connector.test_connection()

    row = await sep_get_connection(ts)
    if row is not None:
        if result.ok:
            row.status = "connected"
            row.verified_at = utcnow()
            row.last_error = None
        else:
            row.status = "error"
            row.last_error = (result.detail or "Connection test failed.")[:500]
        await ts.flush()

    await record_audit(
        ts, "sep.connection.test", actor_user_id=principal.user_id,
        target_type="sep_connection", target_id=row.id if row is not None else "",
        meta={"provider": connector.platform, "ok": result.ok, "detail": result.detail},
    )
    return CRMConnectionTestOut(ok=result.ok, label=result.label, detail=result.detail)


@router.delete("/sep/connection", status_code=status.HTTP_204_NO_CONTENT)
async def clear_sep_connection(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> Response:
    """Disconnect the tenant's SEP. Pushes fall back to the recording stub."""
    removed = await sep_clear_credentials(ts)
    await record_audit(
        ts, "sep.connection.clear", actor_user_id=principal.user_id,
        target_type="sep_connection", meta={"removed": removed},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
