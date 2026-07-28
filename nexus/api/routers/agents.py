"""Agent endpoints: list available agents, run one, or run the full account pipeline."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import AgentRunRequest, AgentRunResponse
from nexus.agents.runtime import available_agents, get_agent_runtime
from nexus.billing.meter import metered
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.pipeline import process_account

router = APIRouter(prefix="/agents", tags=["agents"])

# Agent name -> billed capability. A dict, not branching: adding an agent is configuration.
# Keys are the registered agent names (see nexus.agents.runtime.available_agents).
_AGENT_CAPABILITY = {
    "research": "ai.research_brief",
    "messaging": "ai.email_draft",
    "qa": "ai.account_qa",
    "contact_rec": "ai.contact_rank",
    "call_script": "ai.call_script",
    "scoring": "ai.scoring",
    "discovery": "discovery.account_added",
}
# A newly registered agent still gets metered rather than silently running free.
_DEFAULT_AGENT_CAPABILITY = "ai.chat_turn"


@router.get("", response_model=list[str])
async def list_agents(_: Principal = Depends(require(Permission.run_agents))) -> list[str]:
    return available_agents()


@router.post("/{agent_name}/run", response_model=AgentRunResponse)
async def run_agent(
    agent_name: str,
    body: AgentRunRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> AgentRunResponse:
    if agent_name not in available_agents():
        # Metered *after* this check, so an unknown agent is never billed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent '{agent_name}'")
    capability = _AGENT_CAPABILITY.get(agent_name, _DEFAULT_AGENT_CAPABILITY)
    async with metered(ts, capability, user_id=principal.user_id,
                       attrs={"agent": agent_name}):
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
    principal: Principal = Depends(require(Permission.run_agents)),
) -> dict:
    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    # Full manual refresh: signals + firmographic enrichment + scoring (process_account), then
    # source the buying committee so "Run pipeline" actually populates contacts. Contact sourcing
    # lives here (manual click), not in process_account, so the automated sweep stays cheap.
    async with metered(ts, "workflow.orchestration_run", user_id=principal.user_id,
                       attrs={"account_id": account_id}):
        result = await process_account(ts, account)
        if result.get("icp_screened"):
            # The re-screen archived this account as a definitive ICP non-match — don't source a
            # buying committee for it (contacts would also block any future auto-archive).
            result["new_contacts"] = 0
            return result
        from nexus.campaigns.sourcing import source_account_contacts
        from nexus.core.config import get_settings

        new_contacts = await source_account_contacts(
            ts, account, limit=get_settings().discovery_contacts_per_account
        )
        result["new_contacts"] = len(new_contacts)
        return result
