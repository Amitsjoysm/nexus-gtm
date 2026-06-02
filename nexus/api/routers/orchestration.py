"""Orchestration endpoints: author runs, inspect them, stream progress, decide approvals.

A POST to ``/orchestration/runs`` plans the goal and drives it inline to its first stopping
point (completion or the approval gate), then returns the run snapshot. Live progress is
available over Server-Sent Events at ``/runs/{id}/events`` (resumable via ``Last-Event-ID``).
Outbound work waits at ``/approvals`` until a manager approves or rejects it.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from nexus.api.deps import Principal, get_principal, get_tenant_session, require
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.orchestration.engine import OrchestrationError, get_orchestration_engine
from nexus.orchestration.planner import PlanError
from nexus.orchestration.schemas import (
    ApprovalDecisionRequest,
    ApprovalOut,
    RunCreateRequest,
    RunOut,
)
from nexus.models.orchestration import (
    Approval,
    OrchestrationRun,
    RunEvent,
    RunStep,
    RUN_AWAITING_APPROVAL,
    RUN_TERMINAL,
)
from nexus.workers.tasks import tenant_session

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


async def _load_steps(ts: TenantSession, run_id: str) -> list[RunStep]:
    stmt = ts.select(RunStep, RunStep.run_id == run_id).order_by(RunStep.idx)
    return list((await ts.session.scalars(stmt)).all())


@router.post("/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: RunCreateRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_orchestration)),
) -> RunOut:
    engine = get_orchestration_engine()
    try:
        run = await engine.create_run(
            ts,
            body.goal,
            body.input,
            created_by=principal.user_id,
            idempotency_key=body.idempotency_key,
            account_id=body.account_id,
        )
        # Drive inline to the first stopping point (completion or approval gate).
        await engine.execute_run(ts, run)
    except PlanError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    steps = await _load_steps(ts, run.id)
    return RunOut.from_model(run, steps)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_orchestration)),
) -> list[RunOut]:
    stmt = ts.select(OrchestrationRun).order_by(OrchestrationRun.created_at.desc()).limit(100)
    runs = list((await ts.session.scalars(stmt)).all())
    return [RunOut.from_model(r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_orchestration)),
) -> RunOut:
    run = await ts.get(OrchestrationRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    steps = await _load_steps(ts, run.id)
    return RunOut.from_model(run, steps)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_orchestration)),
) -> RunOut:
    run = await ts.get(OrchestrationRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    await get_orchestration_engine().cancel_run(ts, run)
    steps = await _load_steps(ts, run.id)
    return RunOut.from_model(run, steps)


def _format_sse(seq: int, type_: str, data: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data)}\n\n"


async def _event_stream(
    tenant_id: str, run_id: str, last_seq: int, request: Request
) -> AsyncIterator[str]:
    """Replay-then-follow the run's append-only event log as SSE.

    Each connection opens a short-lived session per poll so it never pins a connection
    for the stream's lifetime. The stream ends when the run reaches a terminal state, or
    when it parks at an approval (nothing more happens until a human acts — the client
    reconnects with ``Last-Event-ID`` after deciding)."""
    idle_polls = 0
    max_polls = 600  # ~5 min ceiling so a stuck stream can't leak forever
    for _ in range(max_polls):
        if await request.is_disconnected():
            return
        async with tenant_session(tenant_id) as ts:
            run = await ts.get(OrchestrationRun, run_id)
            if run is None:
                yield _format_sse(last_seq, "error", {"detail": "run not found"})
                return
            stmt = (
                ts.select(RunEvent, RunEvent.run_id == run_id, RunEvent.seq > last_seq)
                .order_by(RunEvent.seq)
            )
            events = list((await ts.session.scalars(stmt)).all())
            for ev in events:
                yield _format_sse(ev.seq, ev.type, ev.data or {})
                last_seq = ev.seq
            run_status = run.status

        if run_status in RUN_TERMINAL:
            return
        if run_status == RUN_AWAITING_APPROVAL and not events:
            # Parked: emit a heartbeat and close so the client isn't held open.
            yield ": awaiting-approval\n\n"
            return
        idle_polls = idle_polls + 1 if not events else 0
        await asyncio.sleep(0.5)


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    # EventSource can't set Authorization headers, so this endpoint authenticates the
    # same bearer token but we still gate it explicitly here rather than via `require`.
    if not has_permission(principal.role, Permission.run_orchestration):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "lacks run_orchestration")
    try:
        last_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_seq = 0
    return StreamingResponse(
        _event_stream(principal.tenant_id, run_id, last_seq, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    status_filter: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.approve_outreach)),
) -> list[ApprovalOut]:
    where = []
    if status_filter:
        where.append(Approval.status == status_filter)
    stmt = ts.select(Approval, *where).order_by(Approval.created_at.desc()).limit(100)
    rows = list((await ts.session.scalars(stmt)).all())
    return [ApprovalOut.from_model(a) for a in rows]


@router.post("/approvals/{approval_id}/decision", response_model=RunOut)
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.approve_outreach)),
) -> RunOut:
    engine = get_orchestration_engine()
    try:
        run = await engine.decide(
            ts,
            approval_id,
            decision=body.decision,
            edits=body.edits or None,
            decided_by=principal.user_id,
        )
    except OrchestrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    steps = await _load_steps(ts, run.id)
    return RunOut.from_model(run, steps)
