"""Agent endpoints: list available agents, run one, or run the full account pipeline."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

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


class LatestRunOut(BaseModel):
    """The most recent COMPLETED run of one agent — what the page shows instead of an empty card."""

    agent: str
    input: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    created_at: str
    #: Generated today (UTC). Drafts and call scripts propose specific meeting dates computed on
    #: the day they were written; one from yesterday may name days that have passed, so the
    #: composer regenerates rather than reuses it. A brief carries no dates and ignores this.
    fresh: bool


#: How far back to look. Bounded, because the latest-per-agent is found by scanning rather than by a
#: JSON-path query: SQLite and Postgres disagree on JSON syntax, and a filter on `input.contact_id`
#: that raised on one of them would break the page on every load.
_LATEST_SCAN = 200


@router.get("/runs/latest", response_model=dict[str, LatestRunOut])
async def latest_runs(
    account_id: str,
    contact_id: str | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> dict[str, LatestRunOut]:
    """The latest completed result of each agent for this account — or for one contact at it.

    Every run was already SAVED (`AgentRun` holds the full output) and nothing read one back, so a
    brief the customer paid for vanished on reload and "Generate brief" bought it again. Measured:
    Marketjoy held two completed briefs while its page showed an empty card.

    Account-level and contact-level results are kept apart: with `contact_id`, only runs made for
    that contact (the composer's drafts); without it, only runs made for the account as a whole —
    a draft written to one person is never shown as the account's, or as another person's.
    A failed run is never offered: a failure newer than a good brief must not replace it on screen.
    """
    from nexus.core.db import utcnow
    from nexus.models.intelligence import AgentRun

    if await ts.get(Account, account_id) is None:
        # Tenant-scoped lookup, so another workspace's account is simply not found.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    stmt = (
        ts.select(AgentRun, AgentRun.account_id == account_id, AgentRun.status == "completed")
        .order_by(AgentRun.created_at.desc())
        .limit(_LATEST_SCAN)
    )
    today = utcnow().date()
    out: dict[str, LatestRunOut] = {}
    for run in (await ts.session.scalars(stmt)).all():
        if run.agent in out:
            continue
        run_contact = (run.input or {}).get("contact_id")
        if (contact_id or None) != (run_contact or None):
            continue
        out[run.agent] = LatestRunOut(
            agent=run.agent,
            input=run.input or {},
            output=run.output or {},
            created_at=run.created_at.isoformat(),
            fresh=run.created_at.date() == today,
        )
    return out


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

    # Record what the model actually consumed, ALONGSIDE the flat per-action charge above. The flat
    # rate is the bill and is what makes it predictable; this is the evidence that the flat rate is
    # still the right one. `ai.tokens` was priced and metered nowhere until this call site existed.
    #
    # Outside the `metered` block on purpose: a token record must not be part of the transaction
    # that decides whether the customer was allowed to run the agent at all.
    from nexus.billing.tokens import meter_tokens

    await meter_tokens(ts, tokens=getattr(result, "tokens", 0) or 0, agent=agent_name)

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
