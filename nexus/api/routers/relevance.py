"""Relevance Engine endpoints: read/update the tenant's GTM profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import RelevanceProfileIn, RelevanceProfileOut
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
