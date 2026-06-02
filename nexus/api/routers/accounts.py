"""Accounts & contacts: CRUD plus waterfall contact enrichment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import AccountIn, AccountOut, ContactIn, ContactOut
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.enrichment.waterfall import get_enricher
from nexus.models.account import Account, Contact

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _account_out(a: Account) -> AccountOut:
    return AccountOut(
        id=a.id,
        name=a.name,
        domain=a.domain,
        industry=a.industry,
        employee_count=a.employee_count,
        country=a.country,
        tech_stack=a.tech_stack or [],
    )


def _contact_out(c: Contact) -> ContactOut:
    return ContactOut(
        id=c.id,
        account_id=c.account_id,
        full_name=c.full_name,
        title=c.title,
        seniority=c.seniority,
        email=c.email,
        phone=c.phone,
        linkedin_url=c.linkedin_url,
        email_confidence=c.email_confidence,
        phone_confidence=c.phone_confidence,
        enrichment_source=c.enrichment_source,
    )


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    account = Account(
        tenant_id=ts.tenant_id,
        name=body.name,
        domain=body.domain,
        industry=body.industry,
        employee_count=body.employee_count,
        country=body.country,
        tech_stack=body.tech_stack,
    )
    ts.add(account)
    await ts.flush()
    return _account_out(account)


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 100,
) -> list[AccountOut]:
    accounts = await ts.list(Account, limit=limit)
    return [_account_out(a) for a in accounts]


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return _account_out(account)


@router.post(
    "/{account_id}/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED
)
async def create_contact(
    account_id: str,
    body: ContactIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ContactOut:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    contact = Contact(
        tenant_id=ts.tenant_id,
        account_id=account.id,
        full_name=body.full_name,
        title=body.title,
        seniority=body.seniority,
        email=body.email,
        phone=body.phone,
        linkedin_url=body.linkedin_url,
    )
    ts.add(contact)
    await ts.flush()
    return _contact_out(contact)


@router.get("/{account_id}/contacts", response_model=list[ContactOut])
async def list_contacts(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ContactOut]:
    contacts = await ts.list(Contact, Contact.account_id == account_id)
    return [_contact_out(c) for c in contacts]


@router.post("/contacts/{contact_id}/enrich", response_model=ContactOut)
async def enrich_contact(
    contact_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ContactOut:
    contact = await ts.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    await get_enricher().enrich_contact(ts, contact)
    return _contact_out(contact)
