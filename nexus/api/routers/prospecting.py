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


class PopulateRunOut(BaseModel):
    id: str
    status: str
    requested: int
    delivered: int
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
        run = await start(ts, count=body.count, user_id=principal.user_id)
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
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> PopulateRunOut | None:
    """The workspace's most recent populate, so a reloaded page can pick its progress back up."""
    from nexus.models.prospecting import ProspectRun
    from nexus.prospecting.populate import run_out

    run = (await ts.session.scalars(
        ts.select(ProspectRun, ProspectRun.kind == "populate")
        .order_by(ProspectRun.created_at.desc()).limit(1))).first()
    return PopulateRunOut(**run_out(run)) if run is not None else None


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
