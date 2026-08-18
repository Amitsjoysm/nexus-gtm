"""Workspace-wide Contacts list — every contact across the workspace, with its account context
and a free-text filter. (Per-account contacts live under /accounts/{id}/contacts.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, or_, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import ReverifyResult, WorkspaceContactOut
from nexus.billing.meter import metered
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("/reverify", response_model=ReverifyResult)
async def reverify_contact_emails(
    only_unverified: bool = True,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ReverifyResult:
    """Re-run the email verifier against contacts that already have an address but no verdict
    (status null/blank/unknown), and persist the result. Use after the verifier was unreachable
    so guessed addresses get their deliverability status. ``only_unverified=false`` re-checks
    every contact with an email."""
    from nexus.enrichment.reverify import reverify_contacts

    result = await reverify_contacts(ts, only_unverified=only_unverified)

    # Metered AFTER the pass, with the real number of checks: 12 verifications is 12 units, not
    # one call. The size of the batch is not knowable up front, so gating beforehand would have
    # to guess — enforcement instead applies to the next call, which is the honest behavior for
    # a bulk job.
    checked = int(result.get("checked", 0) or 0)
    if checked:
        async with metered(ts, "verify.email", quantity=checked,
                           user_id=principal.user_id, attrs={"bulk": True}):
            pass
    return ReverifyResult(**result)


@router.get("", response_model=list[WorkspaceContactOut])
async def list_contacts(
    q: str | None = None,
    account_id: str | None = None,
    include_deleted: bool = False,
    limit: int = 200,
    offset: int = 0,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[WorkspaceContactOut]:
    """All contacts in the workspace (joined to their account), newest first. ``q`` filters on
    name / title / email / account name; ``account_id`` scopes to one account.

    Filtering and pagination are pushed into SQL: a large workspace returns one page from the
    database instead of loading its entire contact book into memory and slicing in Python."""
    return await _query_contacts(
        ts, q=q, account_id=account_id, include_deleted=include_deleted,
        limit=limit, offset=offset,
    )


async def _query_contacts(
    ts: TenantSession,
    *,
    q: str | None,
    account_id: str | None,
    include_deleted: bool,
    limit: int,
    offset: int,
) -> list[WorkspaceContactOut]:
    """The one contact query. Shared by the list view and the CSV export so the two can never
    disagree — an export that does not match what is on screen is worse than no export."""
    # Tenant scoping is enforced in the query (the only reliable layer — RLS is defense-in-depth
    # and may be bypassed by the DB role). Filter Contact.tenant_id AND Account.tenant_id so a
    # cross-tenant join can never surface another workspace's people.
    stmt = (
        select(Contact, Account.name, Account.domain)
        .join(Account, Account.id == Contact.account_id)
        .where(Contact.tenant_id == ts.tenant_id, Account.tenant_id == ts.tenant_id)
    )
    # Soft-deleted contacts are hidden by default. Filtered in SQL, not in Python, so a page of
    # 200 stays a page of 200 rather than silently shrinking as deleted rows are dropped.
    if not include_deleted:
        stmt = stmt.where(Contact.deleted_at.is_(None))
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
            email_checked_at=(
                contact.email_checked_at.isoformat() if contact.email_checked_at else None
            ),
            email_provider=(contact.custom_fields or {}).get("email_provider"),
            phone=contact.phone,
            phone_confidence=contact.phone_confidence,
            linkedin_url=contact.linkedin_url,
            enrichment_source=contact.enrichment_source,
        )
        for (contact, acc_name, acc_domain) in rows
    ]


@router.delete("/{contact_id}")
async def delete_contact(
    contact_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """Soft-delete a contact.

    The row survives. A contact is referenced by cadence steps, call records and outreach history,
    and removing it would orphan every one of them — so "delete" means "stop showing me this
    person", and the audit trail of what was already sent to them stays intact and explainable.

    Idempotent: deleting an already-deleted contact is a success, so a double-clicked button or a
    retried request does not produce a confusing 404.
    """
    from nexus.core.db import utcnow

    contact = await ts.first(Contact, Contact.id == contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    if contact.deleted_at is None:
        contact.deleted_at = utcnow()
        await ts.flush()
    return {"id": contact_id, "deleted": True, "restorable": True}


@router.post("/{contact_id}/enrich-phone")
async def enrich_contact_phone(
    contact_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """Find this contact's phone number. **Rep-triggered only.**

    There is deliberately no background sweep: each lookup is a paid actor run, so enriching a
    1,000-contact workspace on a schedule is a four-figure bill nobody asked for. The rep clicks the
    person they are about to call. ``NEXUS_PHONE_ENRICH_AUTO`` exists for a future bulk path and is
    off.

    Served from the shared people record when another workspace already bought this number, which is
    invisible to the caller and the whole point — same answer, no second actor run. Metered either
    way: the customer is charged for the answer, not for our infrastructure.
    """
    from nexus.people.enrich import find_phone

    contact = await ts.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    account = await ts.get(Account, contact.account_id) if contact.account_id else None
    linkedin = (getattr(contact, "linkedin_url", "") or "").strip()
    if not linkedin and not (contact.email or "").strip():
        # No shared identity, so nothing to look up. Say so rather than reporting "no phone found",
        # which would read as a fact about the person instead of a gap in what we know.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This contact needs a LinkedIn URL or an email address before a phone can be found.",
        )

    result = await find_phone(
        ts,
        linkedin_url=linkedin,
        email=(contact.email or "").strip(),
        full_name=contact.full_name or "",
        country=(getattr(contact, "country", "") or ""),
        account_country=(getattr(account, "country", "") or "") if account else "",
        user_id=principal.user_id,
    )

    if result.status == "unconfigured":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Phone lookup is not configured. Set NEXUS_APIFY_API_KEY.",
        )

    # Write the number onto the tenant's own contact row: the shared record is the cache, this is
    # the workspace's copy, and a rep must be able to correct it without editing what other
    # tenants see.
    if result.phone and not (contact.phone or "").strip():
        contact.phone = result.phone
        await ts.flush()

    return {
        "contact_id": contact_id,
        "phone": result.phone,
        "raw": result.raw,
        "status": result.status,
        "cached": result.cached,
    }


@router.post("/{contact_id}/restore")
async def restore_contact(
    contact_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """Undo a soft delete. The whole point of not hard-deleting."""
    contact = await ts.first(Contact, Contact.id == contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    contact.deleted_at = None
    await ts.flush()
    return {"id": contact_id, "deleted": False}


@router.get("/export")
async def export_contacts(
    q: str | None = None,
    account_id: str | None = None,
    include_deleted: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """Export the contact book as CSV, honouring the same filters as the list view.

    Streamed through the same query rather than a second code path, so what you export is exactly
    what you were looking at — an export that disagrees with the screen is worse than none.
    """
    from nexus.api.csv_export import csv_response

    rows = await _query_contacts(
        ts, q=q, account_id=account_id, include_deleted=include_deleted, limit=0, offset=0
    )
    return csv_response(
        "contacts.csv",
        ["full_name", "title", "seniority", "email", "email_status", "phone",
         "linkedin_url", "account", "domain"],
        (
            [
                r.full_name, r.title or "", r.seniority or "", r.email or "",
                r.email_status or "", r.phone or "", r.linkedin_url or "",
                r.account_name or "", r.account_domain or "",
            ]
            for r in rows
        ),
    )
