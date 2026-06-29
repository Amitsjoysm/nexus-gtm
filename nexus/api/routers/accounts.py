"""Accounts & contacts: CRUD plus waterfall contact enrichment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    AccountIn,
    AccountOut,
    ContactIn,
    ContactLookalikeOut,
    ContactLookalikeResponse,
    ContactOut,
    LookalikeOut,
    LookalikeResponse,
)
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.enrichment.waterfall import get_enricher
from nexus.integrations.company_search import domain_from_url
from nexus.lookalike import get_lookalike_service
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _account_out(a: Account, *, fit_score: int | None = None) -> AccountOut:
    cf = a.custom_fields or {}
    return AccountOut(
        id=a.id,
        name=a.name,
        domain=a.domain,
        industry=a.industry,
        employee_count=a.employee_count,
        country=a.country,
        tech_stack=a.tech_stack or [],
        fit_score=fit_score,
        linkedin_url=cf.get("linkedin_url"),
        description=cf.get("description"),
        source=a.source,
        crm_source=a.crm_source,
        crm_synced_at=a.crm_synced_at.isoformat() if a.crm_synced_at else None,
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


async def _latest_fit_scores(ts: TenantSession, account_ids: list[str]) -> dict[str, int]:
    """Latest composite (0..100) per account.

    Resolved entirely in SQL with a window function: we transfer exactly one row per account
    (its newest score) instead of loading the account's full score history and sorting in
    Python. Backed by ``ix_score_tenant_account_computed`` so it stays O(accounts), not
    O(accounts × score history), as the scoring history grows under continuous automation."""
    if not account_ids:
        return {}
    ranked = (
        select(
            AccountScore.account_id.label("account_id"),
            AccountScore.composite.label("composite"),
            func.row_number()
            .over(
                partition_by=AccountScore.account_id,
                order_by=(AccountScore.computed_at.desc(), AccountScore.id.desc()),
            )
            .label("rn"),
        )
        .where(
            AccountScore.tenant_id == ts.tenant_id,
            AccountScore.account_id.in_(account_ids),
        )
        .subquery()
    )
    stmt = select(ranked.c.account_id, ranked.c.composite).where(ranked.c.rn == 1)
    rows = (await ts.session.execute(stmt)).all()
    return {account_id: composite for account_id, composite in rows}


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 200,
) -> list[AccountOut]:
    # Newest first so a just-added account (manual, lookalike, or discovery) always lands at the
    # top of the list instead of in undefined order somewhere in the middle.
    stmt = ts.select(Account).order_by(Account.created_at.desc()).limit(limit)
    rows = list((await ts.session.scalars(stmt)).all())
    # Archived accounts (an SDR removed them as not-relevant) are hidden from the working list.
    accounts = [a for a in rows if not (a.custom_fields or {}).get("archived")]
    scores = await _latest_fit_scores(ts, [a.id for a in accounts])
    return [_account_out(a, fit_score=scores.get(a.id)) for a in accounts]


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    scores = await _latest_fit_scores(ts, [account.id])
    return _account_out(account, fit_score=scores.get(account.id))


async def _set_archived(ts: TenantSession, account_id: str, archived: bool) -> AccountOut:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    cf = dict(account.custom_fields or {})
    cf["archived"] = archived
    account.custom_fields = cf
    await ts.flush()
    scores = await _latest_fit_scores(ts, [account.id])
    return _account_out(account, fit_score=scores.get(account.id))


@router.post("/{account_id}/archive", response_model=AccountOut)
async def archive_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    """Remove an account from the working list (soft, recoverable) — for 'not relevant to me'.
    History and any CRM sync are preserved; it simply stops showing in the Accounts list."""
    return await _set_archived(ts, account_id, True)


@router.post("/{account_id}/unarchive", response_model=AccountOut)
async def unarchive_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    return await _set_archived(ts, account_id, False)


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


@router.post("/{account_id}/lookalikes", response_model=LookalikeResponse)
async def find_lookalikes(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 10,
) -> LookalikeResponse:
    """Find companies similar to this account, scored against the tenant's ICP.

    Offline (stub search) this returns an empty list; a keyed Exa provider lights it up.
    """
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    limit = max(1, min(limit, 50))
    found = await get_lookalike_service().find(ts, account, limit=limit)
    return LookalikeResponse(
        seed_account_id=account.id,
        seed_domain=domain_from_url(account.domain),
        lookalikes=[LookalikeOut(**lk.as_dict()) for lk in found],
    )


@router.post("/contacts/{contact_id}/lookalikes", response_model=ContactLookalikeResponse)
async def find_contact_lookalikes(
    contact_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 10,
) -> ContactLookalikeResponse:
    """Find people in the workspace who resemble this contact — similar role/seniority/department at
    a similar company. Ranks existing contacts (deterministic, offline-safe)."""
    contact = await ts.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    from nexus.lookalike.contacts import get_contact_lookalike_service

    found = await get_contact_lookalike_service().find(ts, contact, limit=max(1, min(limit, 50)))
    return ContactLookalikeResponse(
        seed_contact_id=contact.id,
        lookalikes=[ContactLookalikeOut(**lk.as_dict()) for lk in found],
    )


@router.post("/{account_id}/source-contacts", response_model=list[ContactOut])
async def source_contacts(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 5,
) -> list[ContactOut]:
    """Source the buying committee for this account (net-new people, deduped + email-verified).
    Powers the account 'Find contacts' action; returns only the contacts newly added."""
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    from nexus.campaigns.sourcing import source_account_contacts

    created = await source_account_contacts(ts, account, limit=max(1, min(limit, 25)))
    return [_contact_out(c) for c in created]


@router.post("/from-lookalike", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def add_from_lookalike(
    body: AccountIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    """Add a lookalike to the tracked accounts and score it against the ICP in one step. Deduped
    by domain (returns the existing account if already tracked)."""
    dom = domain_from_url(body.domain) if body.domain else None
    if dom:
        # Narrow to candidate rows in SQL (domain contains the registrable domain) instead of
        # loading the tenant's entire account book to normalize-compare in Python. The exact
        # normalized match below is still the authority; the LIKE is just a cheap prefilter.
        esc = dom.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        candidates = await ts.list(Account, Account.domain.ilike(f"%{esc}%", escape="\\"))
        for a in candidates:
            if a.domain and domain_from_url(a.domain) == dom:
                # Re-adding a company the rep previously removed must bring it back into the
                # working list — otherwise "Add" returns a still-hidden row and looks like a no-op.
                cf = dict(a.custom_fields or {})
                if cf.get("archived"):
                    cf["archived"] = False
                    a.custom_fields = cf
                    await ts.flush()
                scores = await _latest_fit_scores(ts, [a.id])
                return _account_out(a, fit_score=scores.get(a.id))
    account = Account(
        tenant_id=ts.tenant_id, name=body.name, domain=body.domain, industry=body.industry,
        employee_count=body.employee_count, country=body.country, tech_stack=body.tech_stack,
        source="lookalike",
    )
    ts.add(account)
    await ts.flush()
    # Enrich firmographics + score so the new account lands with a Fit score (no contact sourcing
    # here — that's an explicit follow-up action).
    from nexus.pipeline import process_account

    await process_account(ts, account)
    scores = await _latest_fit_scores(ts, [account.id])
    return _account_out(account, fit_score=scores.get(account.id))


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
    # Social insights for person-level personalization (no-op under the stub; lights up with Apify).
    from nexus.personalization.provider import refresh_person_insights

    await refresh_person_insights(ts, contact)
    return _contact_out(contact)


@router.post("/{account_id}/enrich", response_model=AccountOut)
async def enrich_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    """Fill blank firmographics (industry/size/country/tech/description) from the web on demand.
    Uses Exa when keyed, else DuckDuckGo, then the LLM; existing values are never overwritten."""
    from nexus.enrichment.account import get_account_enricher

    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    await get_account_enricher().enrich(account)
    await ts.flush()
    return _account_out(account)
