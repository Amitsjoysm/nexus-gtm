"""Agent endpoints: list available agents, run one, or run the full account pipeline."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import AgentRunRequest, AgentRunResponse
from nexus.agents.runtime import available_agents, get_agent_runtime
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.pipeline import process_account

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[str])
async def list_agents(_: Principal = Depends(require(Permission.run_agents))) -> list[str]:
    return available_agents()


@router.post("/{agent_name}/run", response_model=AgentRunResponse)
async def run_agent(
    agent_name: str,
    body: AgentRunRequest,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> AgentRunResponse:
    if agent_name not in available_agents():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent '{agent_name}'")
    result = await get_agent_runtime().run(
        agent_name, ts, account_id=body.account_id, **body.inputs
    )
    return AgentRunResponse(
        agent=result.agent,
        status=result.status,
        output=result.output,
        error=result.error,
        latency_ms=result.latency_ms,
        tokens=result.tokens,
        run_id=result.run_id,
    )


@router.post("/pipeline/{account_id}")
async def run_pipeline(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> dict:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return await process_account(ts, account)
