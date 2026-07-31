"""Relevance Engine endpoints: read/update the tenant's GTM profile."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    AnalyzeWebsiteIn,
    RelevanceProfileIn,
    RelevanceProfileOut,
    SuggestTitlesIn,
    TitleRecommendationIn,
    TitleRecommendationOut,
)
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.relevance.engine import get_or_create_profile

logger = logging.getLogger("nexus.api.relevance")

router = APIRouter(prefix="/relevance", tags=["relevance"])


@router.get("/profile", response_model=RelevanceProfileOut)
async def read_profile(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> RelevanceProfileOut:
    profile = await get_or_create_profile(ts)
    return RelevanceProfileOut(
        id=profile.id,
        icp=profile.icp,
        value_props=profile.value_props,
        product_context=profile.product_context,
    )


@router.put("/profile", response_model=RelevanceProfileOut)
async def update_profile(
    body: RelevanceProfileIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> RelevanceProfileOut:
    profile = await get_or_create_profile(ts)
    icp_changed = (profile.icp or {}) != (body.icp or {})
    profile.icp = body.icp
    profile.value_props = body.value_props
    profile.product_context = body.product_context
    await ts.flush()
    # Saving an ICP is the moment someone expects accounts to start appearing. Without this the
    # first batch waits for the daily discovery heartbeat — up to 24 hours of an empty Accounts
    # list, which reads as a broken product rather than a scheduled job.
    if icp_changed and body.icp:
        await _kick_off_discovery(ts)
    return RelevanceProfileOut(
        id=profile.id,
        icp=profile.icp,
        value_props=profile.value_props,
        product_context=profile.product_context,
    )


async def _kick_off_discovery(ts) -> None:
    """Clear the daily stamp and enqueue discovery so the first batch starts now.

    Clearing ``icp_discovery_last_run_at`` is what lets the handler through: it is the idempotency
    guard that stops the heartbeat re-running discovery every tick, and a freshly-defined ICP is
    exactly the case where re-running is correct.

    Best-effort — a queue failure must not fail the ICP save that succeeded. The heartbeat picks it
    up on the next tick regardless, so the worst case is the old behaviour rather than a lost ICP.
    """
    try:
        from nexus.models.identity import Tenant
        from nexus.workers.tasks import enqueue_discover_icp_accounts

        tenant = await ts.session.get(Tenant, ts.tenant_id)
        if tenant is not None:
            tenant.icp_discovery_last_run_at = None
            await ts.flush()
        await enqueue_discover_icp_accounts()
    except Exception:
        logger.warning("could not kick off ICP discovery for %s", ts.tenant_id, exc_info=True)


@router.post("/title-recommendations", response_model=list[TitleRecommendationOut])
async def recommend_target_titles(
    body: TitleRecommendationIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> list[TitleRecommendationOut]:
    """Recommend which buying-committee titles to target for an account, ranked with a reason,
    confidence, department, and buying influence. Read-only and additive — it does not change any
    account/contact/ICP data. Firmographics come from ``account_id`` (if given) and/or the request
    body; the tenant's ICP ``buyer_titles`` bias the ranking toward roles they already pursue."""
    from nexus.relevance.titles import recommend_titles

    industry, employee_count, tech_stack = body.industry, body.employee_count, body.tech_stack
    if body.account_id:
        from nexus.models.account import Account

        account = await ts.get(Account, body.account_id)
        if account is not None:
            industry = industry or account.industry
            employee_count = employee_count or account.employee_count
            tech_stack = tech_stack or (account.tech_stack or [])

    profile = await get_or_create_profile(ts)
    icp = profile.icp or {}
    buyer_titles = icp.get("buyer_titles") or icp.get("titles") or []
    recs = recommend_titles(
        industry=industry,
        employee_count=employee_count,
        tech_stack=tech_stack,
        icp_buyer_titles=buyer_titles,
        department=body.department,
        limit=body.limit,
    )
    return [TitleRecommendationOut(**vars(r)) for r in recs]


@router.post("/suggest-titles", response_model=list[TitleRecommendationOut])
async def suggest_buyer_titles(
    body: SuggestTitlesIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_agents)),
) -> list[TitleRecommendationOut]:
    """Generate up to 10 buyer titles for the whole ICP (all target industries + size + tech), so a
    founder/SDR can populate ``buyer_titles``. Uses the posted draft ICP, or the saved profile when
    the body is empty. Read-only and additive."""
    from nexus.relevance.titles import recommend_titles_for_icp

    icp = {
        "industries": body.industries,
        "employee_min": body.employee_min,
        "employee_max": body.employee_max,
        "required_tech": body.required_tech,
        "buyer_titles": body.buyer_titles,
    }
    if not any([body.industries, body.employee_min, body.employee_max,
                body.required_tech, body.buyer_titles]):
        profile = await get_or_create_profile(ts)
        icp = profile.icp or {}
    recs = recommend_titles_for_icp(icp, limit=body.limit)
    return [TitleRecommendationOut(**vars(r)) for r in recs]


@router.post("/analyze-website", response_model=RelevanceProfileIn)
async def analyze_website(
    body: AnalyzeWebsiteIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> RelevanceProfileIn:
    """AI-draft an ICP from a company's website: web research + LLM inference of who they sell to.
    Returns an editable draft (icp / value_props / product_context) — it does NOT save. The user
    reviews and edits it, then saves via ``PUT /relevance/profile``. Empty draft when offline or
    the site can't be analyzed."""
    from nexus.agents.llm import get_llm_provider
    from nexus.integrations.registry import get_registry
    from nexus.relevance.website_icp import analyze_website_to_icp

    draft = await analyze_website_to_icp(
        body.url, search=get_registry().search, llm=get_llm_provider()
    )
    return RelevanceProfileIn(**draft)
