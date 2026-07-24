"""Relevance Engine endpoints: read/update the tenant's GTM profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import (
    RelevanceProfileIn,
    RelevanceProfileOut,
    TitleRecommendationIn,
    TitleRecommendationOut,
)
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.relevance.engine import get_or_create_profile

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
    profile.icp = body.icp
    profile.value_props = body.value_props
    profile.product_context = body.product_context
    await ts.flush()
    return RelevanceProfileOut(
        id=profile.id,
        icp=profile.icp,
        value_props=profile.value_props,
        product_context=profile.product_context,
    )


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
