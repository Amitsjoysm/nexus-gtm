"""Integration endpoints: CRM sync/push (Salesforce/HubSpot) and SEP push (Outreach/Salesloft).

CRM connectors are bi-directional. Inbound (``/crm/sync``) pulls 1st-party accounts and upserts
them by ``(tenant, crm_source, crm_id)``; the endpoint accepts records inline so the integration
is exercisable offline and in tests. Outbound (``/crm/push/{account_id}``) writes a NEXUS-enriched
account and its contacts back to the configured CRM. Real deployments swap the stub's body for an
authenticated API call without changing either surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, or_, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    CRMPushResponse,
    CRMSyncRequest,
    CRMSyncResponse,
    CRMSyncStatusOut,
    SEPPushRequest,
    SEPPushResponse,
)
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import (
    CRMAccount,
    CRMConnector,
    get_crm_connector,
)
from nexus.ingestion.crm_sync import sync_account_to_crm
from nexus.integrations.sep import get_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant

router = APIRouter(prefix="/integrations", tags=["integrations"])

CRM_SOURCES = ("salesforce", "hubspot")


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
        connector = get_crm_connector()
        if connector.source != body.source:
            configured = get_settings().crm_provider or "stub"
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"This deployment is not connected to {body.source} "
                f"(NEXUS_CRM_PROVIDER is '{configured}'), so there is nothing to pull. "
                f"Post accounts to import them manually, or configure the connector.",
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
        ts, account, connector=get_crm_connector(), now=utcnow()
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
        provider=(settings.crm_provider or "stub"),
        pending=pending,
        synced=total - pending,
    )


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
    result = await get_sep_connector().push_contact(
        sequence=body.sequence, email=email, payload=body.payload
    )
    return SEPPushResponse(ok=result.ok, platform=result.platform, detail=result.detail)
