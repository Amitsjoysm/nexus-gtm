"""Populate a workspace with ICP companies: "how many companies now?" (``nexus/prospecting``)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.api.prospecting")

router = APIRouter(prefix="/discovery", tags=["discovery"])


class PopulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=500)
    #: The service to find companies for (W8); None or "default" is the main service.
    service_id: str | None = None


class PopulateRunOut(BaseModel):
    id: str
    status: str
    requested: int
    delivered: int
    service_id: str | None = None
    sources: dict = Field(default_factory=dict)
    discarded: dict = Field(default_factory=dict)
    notes: dict = Field(default_factory=dict)
    account_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None


class PopulateQuoteOut(BaseModel):
    count: int
    credits_per_company: float
    total_credits: float
    balance: float | None = None
    enough: bool


@router.get("/populate/quote", response_model=PopulateQuoteOut)
async def populate_quote(
    count: int = Query(ge=1, le=500),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> PopulateQuoteOut:
    """What ``count`` companies would cost and whether the balance covers it."""
    from nexus.prospecting.populate import quote

    return PopulateQuoteOut(**await quote(ts, count))


@router.post("/populate", response_model=PopulateRunOut, status_code=202)
async def start_populate(
    body: PopulateIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_relevance)),
) -> PopulateRunOut:
    """Add ``count`` ICP companies to this workspace, charged per company delivered.

    402 before anything is bought when the balance cannot cover the whole count; 409 while another
    populate is running for this workspace.
    """
    from nexus.prospecting.populate import PopulateRefused, run_out, start

    try:
        run = await start(ts, count=body.count, user_id=principal.user_id,
                          service_id=body.service_id)
    except PopulateRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    out = PopulateRunOut(**run_out(run))
    # Committed BEFORE the job is queued, so a worker that picks it up at once can see the run.
    # Nothing is read after this: under RLS the tenant binding ends with the transaction.
    await ts.session.commit()
    try:
        from nexus.workers.tasks import enqueue_populate_accounts

        await enqueue_populate_accounts(ts.tenant_id, out.id)
    except Exception:
        logger.warning("could not enqueue populate %s", out.id, exc_info=True)
    return out


@router.get("/populate/latest", response_model=PopulateRunOut | None)
async def latest_populate(
    service_id: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> PopulateRunOut | None:
    """The workspace's most recent populate, so a reloaded page can pick its progress back up.
    With ``service_id``, that service's (``default`` for the main service); without, any."""
    from nexus.models.prospecting import ProspectRun
    from nexus.prospecting.populate import run_out
    from nexus.relevance.services import normalise_id

    where = [ProspectRun.kind == "populate"]
    if service_id is not None:
        wanted = normalise_id(service_id)
        where.append(ProspectRun.service_id.is_(None) if wanted is None
                     else ProspectRun.service_id == wanted)
    run = (await ts.session.scalars(
        ts.select(ProspectRun, *where)
        .order_by(ProspectRun.created_at.desc()).limit(1))).first()
    return PopulateRunOut(**run_out(run)) if run is not None else None


# ---- the exclusion list (2026-11-19) -----------------------------------------------------------

#: Entries one request may carry; the screen sends a long list in slices of this size.
MAX_EXCLUSIONS_PER_REQUEST = 5000
#: Unreadable entries named back, so a pasted file's mistakes can be found without a flood.
MAX_UNREADABLE_SHOWN = 50


class ExclusionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Domains, websites, ``@domain`` or addresses at the company, as pasted or read from a file.
    entries: list[str] = Field(min_length=1, max_length=MAX_EXCLUSIONS_PER_REQUEST)


class ExclusionsAddedOut(BaseModel):
    added: int
    already: int
    unreadable: list[str]
    unreadable_count: int
    total: int


class ExclusionOut(BaseModel):
    domain: str
    reason: str
    created_at: str | None = None


class ExclusionListOut(BaseModel):
    total: int
    items: list[ExclusionOut]


class ExclusionsRemoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(min_length=1, max_length=MAX_EXCLUSIONS_PER_REQUEST)


@router.get("/exclusions", response_model=ExclusionListOut)
async def list_exclusions(
    q: str = Query("", max_length=255),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ExclusionListOut:
    """Companies discovery never offers this workspace: the exclusion list, and accounts deleted
    with "Don't suggest these again". Newest first; ``q`` matches part of the domain."""
    from nexus.accounts.suppression import exclusions

    total, rows = await exclusions(ts, q=q, limit=limit, offset=offset)
    return ExclusionListOut(total=total, items=[
        ExclusionOut(domain=r.domain, reason=r.reason,
                     created_at=r.created_at.isoformat() if r.created_at else None)
        for r in rows])


@router.post("/exclusions", response_model=ExclusionsAddedOut)
async def add_exclusions(
    body: ExclusionsIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_relevance)),
) -> ExclusionsAddedOut:
    """Put companies on the exclusion list, so Populate, the daily discovery, the Orchestrator and
    every other discovery path never offer them. Accounts the workspace holds are not touched.
    Free. An entry that names no company domain (free mail, a reserved name, nonsense) is reported
    back rather than guessed at."""
    from nexus.accounts.suppression import REASON_EXCLUDED, read_entry, suppress, suppressed

    readable: list[str] = []
    unreadable: list[str] = []
    for raw in body.entries:
        domain = read_entry(raw)
        if domain:
            readable.append(domain)
        elif str(raw or "").strip():
            unreadable.append(str(raw).strip()[:120])
    unique = sorted(set(readable))
    already = len(await suppressed(ts, unique))
    added = await suppress(ts, unique, user_id=principal.user_id, reason=REASON_EXCLUDED)
    from nexus.accounts.suppression import exclusions

    total, _rows = await exclusions(ts, limit=1)
    return ExclusionsAddedOut(added=added, already=already,
                              unreadable=unreadable[:MAX_UNREADABLE_SHOWN],
                              unreadable_count=len(unreadable), total=total)


@router.post("/exclusions/remove")
async def remove_exclusions(
    body: ExclusionsRemoveIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> dict:
    """Take companies off the list; discovery may offer them again."""
    from nexus.accounts.suppression import unsuppress

    return {"removed": await unsuppress(ts, body.domains)}


@router.get("/populate/{run_id}", response_model=PopulateRunOut)
async def get_populate(
    run_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> PopulateRunOut:
    from nexus.models.prospecting import ProspectRun
    from nexus.prospecting.populate import run_out

    run = await ts.get(ProspectRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such populate run.")
    return PopulateRunOut(**run_out(run))
