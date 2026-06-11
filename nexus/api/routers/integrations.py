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
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import (
    CRMAccount,
    HubSpotConnector,
    SalesforceConnector,
    get_crm_connector,
)
from nexus.integrations.sep import get_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant

router = APIRouter(prefix="/integrations", tags=["integrations"])

_CRM_CONNECTORS = {"salesforce": SalesforceConnector, "hubspot": HubSpotConnector}


@router.post("/crm/sync", response_model=CRMSyncResponse)
async def crm_sync(
    body: CRMSyncRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CRMSyncResponse:
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
    connector = _CRM_CONNECTORS[body.source](sample=sample)
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
    result = await get_crm_connector().push_account(account, contacts=contacts)
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
