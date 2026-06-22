"""Workspace-wide Contacts list — every contact across the workspace, with its account context
and a free-text filter. (Per-account contacts live under /accounts/{id}/contacts.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import ReverifyResult, WorkspaceContactOut
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("/reverify", response_model=ReverifyResult)
async def reverify_contact_emails(
    only_unverified: bool = True,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ReverifyResult:
    """Re-run the email verifier against contacts that already have an address but no verdict
    (status null/blank/unknown), and persist the result. Use after the verifier was unreachable
    so guessed addresses get their deliverability status. ``only_unverified=false`` re-checks
    every contact with an email."""
    from nexus.enrichment.reverify import reverify_contacts

    result = await reverify_contacts(ts, only_unverified=only_unverified)
    return ReverifyResult(**result)


@router.get("", response_model=list[WorkspaceContactOut])
async def list_contacts(
    q: str | None = None,
    account_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[WorkspaceContactOut]:
    """All contacts in the workspace (joined to their account), newest first. ``q`` filters on
    name / title / email / account name; ``account_id`` scopes to one account."""
    # Tenant scoping is enforced in the query (the only reliable layer — RLS is defense-in-depth
    # and may be bypassed by the DB role). Filter Contact.tenant_id AND Account.tenant_id so a
    # cross-tenant join can never surface another workspace's people.
    stmt = (
        select(Contact, Account.name, Account.domain)
        .join(Account, Account.id == Contact.account_id)
        .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id)
        .order_by(Contact.created_at.desc())
    )
    if account_id:
        stmt = stmt.where(Contact.account_id == account_id)
    rows = (await ts.session.execute(stmt)).all()

    needle = (q or "").strip().lower()
    out: list[WorkspaceContactOut] = []
    for contact, acc_name, acc_domain in rows:
        if needle:
            hay = " ".join(
                str(v or "")
                for v in (contact.full_name, contact.title, contact.email, acc_name)
            ).lower()
            if needle not in hay:
                continue
        out.append(
            WorkspaceContactOut(
                id=contact.id,
                account_id=contact.account_id,
                account_name=acc_name,
                account_domain=acc_domain,
                full_name=contact.full_name,
                title=contact.title,
                seniority=contact.seniority,
                email=contact.email,
                email_status=contact.email_status,
                email_confidence=contact.email_confidence,
                phone=contact.phone,
                phone_confidence=contact.phone_confidence,
                linkedin_url=contact.linkedin_url,
                enrichment_source=contact.enrichment_source,
            )
        )
    return out[offset : offset + limit] if limit > 0 else out[offset:]
