"""Accounts & contacts: CRUD plus waterfall contact enrichment."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select

from pydantic import BaseModel

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

logger = logging.getLogger("nexus.api.accounts")

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
        sub_industry=cf.get("sub_industry"),
        revenue=cf.get("revenue"),
        # The stored COLUMN wins, with the enrichment value as the fallback. `region` predates the
        # column and was populated only by enrichment into `custom_fields`; reading just one source
        # would either hide what an import wrote or throw away what enrichment found. Column first,
        # because it is what the operator typed or imported.
        region=a.region or cf.get("region"),
        postal_code=a.postal_code or cf.get("postal_code"),
        # Deliberately NOT folded into `revenue`: that is an enrichment BAND ("$10M-$50M") and this
        # is an exact figure the ICP scores a numeric range against. Merging them would make the
        # field mean two different things depending on where the value came from.
        annual_revenue=a.annual_revenue,
        city=cf.get("city"),
        keywords=cf.get("keywords") or [],
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
        email_status=c.email_status,
        email_confidence=c.email_confidence,
        email_checked_at=(c.email_checked_at.isoformat() if c.email_checked_at else None),
        email_provider=(c.custom_fields or {}).get("email_provider"),
        phone_confidence=c.phone_confidence,
        enrichment_source=c.enrichment_source,
    )


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    from nexus.accounts.dedupe import find_existing_account, normalise_on_write

    # The path a rep uses most had no duplicate check at all, and the ones that did compared raw
    # domain strings — so acme.com, www.acme.com and https://acme.com/ became three Acme rows, each
    # scored separately, each raising its own inbox task for the same funding round.
    existing = await find_existing_account(ts, domain=body.domain, name=body.name)
    if existing is not None:
        # 409 rather than silently returning the existing row: the rep asked to create something
        # and needs to know they did not. The id lets the UI offer "open Acme" instead of a dead
        # error. Archived counts as existing — re-adding should restore the notes, not fork them.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "duplicate_account",
                "message": f"{existing.name} is already in this workspace.",
                "account_id": existing.id,
                "archived": bool(getattr(existing, "archived_at", None)),
            },
        )

    account = Account(
        tenant_id=ts.tenant_id,
        name=body.name,
        # Stored normalised so the next comparison is exact and the same company cannot be written
        # four ways.
        domain=normalise_on_write(body.domain),
        industry=body.industry,
        employee_count=body.employee_count,
        country=body.country,
        tech_stack=body.tech_stack,
    )
    ts.add(account)
    await ts.flush()
    # Collect signals for it now rather than waiting up to 6 hours for the refresh sweep. Adding an
    # account is the moment a rep is looking at it, and an empty timeline then reads as "this
    # product does not work" — the observed "zero signals after 30 minutes".
    #
    # Enqueued, never inline: ingestion makes ~10 outbound HTTP calls, and a POST that blocks on a
    # live crawl would take tens of seconds and fail whenever a provider is slow.
    await _enqueue_first_ingest(ts.tenant_id, account.id)
    return _account_out(account)


async def _enqueue_first_ingest(tenant_id: str, account_id: str) -> None:
    """Best-effort first crawl. A queue failure must not fail the account creation that succeeded."""
    try:
        from nexus.workers.tasks import enqueue_process_account

        await enqueue_process_account(tenant_id, account_id)
    except Exception:
        logger.warning("could not enqueue first ingest for %s", account_id, exc_info=True)


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
    response: Response,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AccountOut]:
    """Active (non-archived) accounts, newest first, paginated in SQL.

    The archived filter is applied in the query — not by dropping rows from a fetched page in
    Python, which silently returned fewer than ``limit`` results. ``X-Total-Count`` reports the
    full active count so a client can page through all of them (``offset`` is additive; default
    behavior — first 200, no offset — is unchanged for existing callers such as the SPA).
    """
    active = ts.select(Account).where(Account.archived_at.is_(None))
    total = await ts.session.scalar(select(func.count()).select_from(active.subquery()))
    response.headers["X-Total-Count"] = str(total or 0)
    stmt = active.order_by(Account.created_at.desc()).limit(limit).offset(offset)
    rows = list((await ts.session.scalars(stmt)).all())
    scores = await _latest_fit_scores(ts, [a.id for a in rows])
    return [_account_out(a, fit_score=scores.get(a.id)) for a in rows]


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
    account.set_archived(archived)  # dual-writes archived_at column + legacy JSON mirror
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


class MergeIn(BaseModel):
    model_config = {"extra": "forbid"}

    loser_id: str


@router.post("/{account_id}/merge")
async def merge_duplicate_account(
    account_id: str,
    body: MergeIn,
    ts: TenantSession = Depends(get_tenant_session),
    # Manager+, not rep: a merge rewrites which row holds a whole timeline, and it is not something
    # to undo casually. Reps report duplicates; managers resolve them.
    _: Principal = Depends(require(Permission.manage_plays)),
) -> dict:
    """Fold another account into this one. ``account_id`` is the winner and survives.

    References move, blanks fill, and the loser is archived rather than deleted — the signals,
    alerts, tasks and contacts that explain why somebody was contacted have to outlive the tidy-up.
    """
    from nexus.accounts.merge import merge_accounts

    try:
        report = await merge_accounts(ts, winner_id=account_id, loser_id=body.loser_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return report.as_dict()


class TransferIn(BaseModel):
    model_config = {"extra": "forbid"}

    from_user_id: str
    to_user_id: str


@router.post("/transfer-ownership")
async def transfer_account_ownership(
    body: TransferIn,
    ts: TenantSession = Depends(get_tenant_session),
    # Reassigning somebody else's queue is an admin act.
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> dict:
    """Move one person's open inbox tasks to another. For when somebody leaves.

    Only open work moves. Reassigning completed history would rewrite who did what, which is the
    audit trail rather than a queue.
    """
    from nexus.accounts.merge import transfer_ownership

    return await transfer_ownership(
        ts, from_user_id=body.from_user_id, to_user_id=body.to_user_id
    )


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
                if a.is_archived:
                    a.set_archived(False)  # clears archived_at + legacy JSON mirror
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
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ContactOut:
    contact = await ts.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    # Re-verification cool-down: a confirmed-valid address doesn't decay in 30 days, and repeat
    # clicks must not burn verifier quota. 429 tells the SDR exactly when the next check is due.
    from datetime import timedelta

    from nexus.core.config import get_settings
    from nexus.core.db import ensure_aware, utcnow

    checked = ensure_aware(contact.email_checked_at)
    cooldown = timedelta(days=get_settings().email_reverify_cooldown_days)
    if contact.email_status == "valid" and checked is not None and utcnow() - checked < cooldown:
        next_due = checked + cooldown
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This email was verified valid on {checked:%d %b %Y}; "
            f"re-verification is available on {next_due:%d %b %Y}.",
        )
    # A person pressed Enrich, so a quota block is theirs to see: `raise_on_block` turns it into
    # the 402 carrying the upsell rather than a silent no-op that looks like "nothing was found".
    await get_enricher().enrich_contact(
        ts, contact, user_id=principal.user_id, raise_on_block=True
    )
    # Fill the LinkedIn profile URL from web search (Exa) when blank — additive, never overwrites.
    from nexus.enrichment.linkedin import enrich_contact_linkedin

    await enrich_contact_linkedin(ts, contact)
    # Social insights for person-level personalization (no-op under the stub; lights up with Apify).
    from nexus.personalization.provider import refresh_person_insights

    await refresh_person_insights(ts, contact)
    return _contact_out(contact)


@router.post("/{account_id}/enrich", response_model=AccountOut)
async def enrich_account(
    account_id: str,
    response: Response,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> AccountOut:
    """Fill blank firmographics/technographics (industry, size, country, tech, sub-industry,
    revenue, HQ region/city, description, LinkedIn, keywords) on demand. A registered source
    database is read first when one holds the domain; otherwise Exa when keyed, else DuckDuckGo,
    then the LLM. Existing values are never overwritten. The list of fields actually filled is
    returned in the ``X-Enriched-Fields`` header so the UI can report honestly ("Added revenue,
    keywords" vs "No new public data found") instead of a blanket success.

    Billed as ``enrich.account``; over quota this is a 402 carrying the upsell, because the person
    who clicked needs to know why nothing happened."""
    from nexus.enrichment.account import get_account_enricher

    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    filled = await get_account_enricher().enrich(
        # `force=True`: a PERSON pressed Enrich. The attempt interval exists to stop a background
        # sweep re-buying the same empty answer every refresh cycle, never to tell a user who asked
        # explicitly that nothing happened — which reads as a broken button, not as a saving.
        # Same distinction `raise_on_block=True` already draws on the line above.
        ts, account, user_id=principal.user_id, raise_on_block=True, force=True,
    )
    await ts.flush()
    response.headers["X-Enriched-Fields"] = ",".join(filled)
    return _account_out(account)


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> dict:
    """Soft-delete an account, reusing the existing archive mechanism.

    Deliberately NOT a row delete. Signals, alerts, inbox tasks, cadence steps and CRM links all
    reference the account; removing it would orphan every one of them and break the timeline that
    explains why anyone was contacted. `set_archived` is the single write-point (it dual-writes the
    legacy `custom_fields['archived']` flag), so this reuses it rather than inventing a second
    notion of "gone" that the list views would then have to learn about.

    Idempotent — a double-clicked button is a success, not a 404.
    """
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if not account.is_archived:
        account.set_archived(True, reason="deleted by user")
        await ts.flush()
    return {"id": account_id, "deleted": True, "restorable": True}


@router.get("/export/csv")
async def export_accounts(
    include_archived: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """Export accounts as CSV.

    Mounted at an explicit `/export/csv` path rather than `/export`: the router already has a
    `/{account_id}` route, and a bare `/export` would be ambiguous with an account whose id happens
    to be "export".
    """
    from sqlalchemy import select

    from nexus.api.csv_export import csv_response, csv_timestamp

    stmt = select(Account).where(Account.tenant_id == ts.tenant_id)
    if not include_archived:
        stmt = stmt.where(Account.archived_at.is_(None))
    rows = (await ts.session.scalars(stmt.order_by(Account.created_at.desc()))).all()

    return csv_response(
        "accounts.csv",
        ["name", "domain", "industry", "employee_count", "country", "tech_stack",
         "archived", "created_at"],
        (
            [
                a.name, a.domain or "", a.industry or "", a.employee_count or "",
                a.country or "", "; ".join(a.tech_stack or []),
                "yes" if a.is_archived else "no",
                csv_timestamp(a.created_at),
            ]
            for a in rows
        ),
    )
