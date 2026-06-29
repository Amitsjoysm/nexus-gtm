"""Workspace-wide Contacts list — every contact across the workspace, with its account context
and a free-text filter. (Per-account contacts live under /accounts/{id}/contacts.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select

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
    name / title / email / account name; ``account_id`` scopes to one account.

    Filtering and pagination are pushed into SQL: a large workspace returns one page from the
    database instead of loading its entire contact book into memory and slicing in Python."""
    # Tenant scoping is enforced in the query (the only reliable layer — RLS is defense-in-depth
    # and may be bypassed by the DB role). Filter Contact.tenant_id AND Account.tenant_id so a
    # cross-tenant join can never surface another workspace's people.
    stmt = (
        select(Contact, Account.name, Account.domain)
        .join(Account, Account.id == Contact.account_id)
        .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id)
    )
    if account_id:
        stmt = stmt.where(Contact.account_id == account_id)

    needle = (q or "").strip().lower()
    if needle:
        # Escape LIKE wildcards so a literal % or _ in the query is matched as text, not a pattern.
        esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        stmt = stmt.where(
            or_(
                func.lower(Contact.full_name).like(like, escape="\\"),
                func.lower(func.coalesce(Contact.title, "")).like(like, escape="\\"),
                func.lower(func.coalesce(Contact.email, "")).like(like, escape="\\"),
                func.lower(Account.name).like(like, escape="\\"),
            )
        )

    stmt = stmt.order_by(Contact.created_at.desc())
    if offset > 0:
        stmt = stmt.offset(offset)
    if limit > 0:
        stmt = stmt.limit(limit)

    rows = (await ts.session.execute(stmt)).all()
    return [
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
        for (contact, acc_name, acc_domain) in rows
    ]
