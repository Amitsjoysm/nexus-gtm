"""Outcome-feedback loop endpoints: capture results, read learned relevance weights.

Reps record outcomes (``manage_accounts``); managers read the attribution summary
(``view_analytics``). The learned-weights read is available to anyone who can run agents so the
relevance UI can show whether scoring is still on the static defaults or has started to learn.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    LearnedWeightsOut,
    OutcomeIn,
    OutcomeOut,
    OutcomeSummaryOut,
)
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.models.outcome import Outcome
from nexus.outcomes.service import STAGES, get_outcome_service

router = APIRouter(prefix="/outcomes", tags=["outcomes"])


def _outcome_out(o: Outcome) -> OutcomeOut:
    return OutcomeOut(
        id=o.id,
        stage=o.stage,
        account_id=o.account_id,
        contact_id=o.contact_id,
        industry=o.industry,
        employee_count=o.employee_count,
        country=o.country,
        tech_count=o.tech_count,
        created_at=o.created_at.isoformat(),
    )


@router.post("", response_model=OutcomeOut, status_code=status.HTTP_201_CREATED)
async def record_outcome(
    body: OutcomeIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> OutcomeOut:
    if body.stage not in STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown outcome stage '{body.stage}'; expected one of {', '.join(STAGES)}",
        )
    account: Account | None = None
    if body.account_id is not None:
        account = await ts.get(Account, body.account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    outcome = await get_outcome_service().record(
        ts,
        stage=body.stage,
        account=account,
        account_id=body.account_id,
        contact_id=body.contact_id,
        meta=body.meta,
    )
    return _outcome_out(outcome)


@router.get("", response_model=list[OutcomeOut])
async def list_outcomes(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    limit: int = 50,
) -> list[OutcomeOut]:
    rows = await get_outcome_service().list_recent(ts, limit=limit)
    return [_outcome_out(o) for o in rows]


@router.get("/weights", response_model=LearnedWeightsOut)
async def learned_weights(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> LearnedWeightsOut:
    lw = await get_outcome_service().learned_weights(ts)
    return LearnedWeightsOut(**lw.as_dict())


@router.get("/summary", response_model=OutcomeSummaryOut)
async def outcome_summary(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.view_analytics)),
) -> OutcomeSummaryOut:
    return OutcomeSummaryOut(**await get_outcome_service().summary(ts))
