"""Offline tests for the Segment Campaign Engine (sub-project A)."""
from __future__ import annotations

from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_DRAFT_PENDING,
    TARGET_PENDING,
)
from tests.conftest import make_tenant, tenant_session


async def test_campaign_defaults():
    """Column defaults populate on flush, the codebase idiom for persisted rows."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        c = Campaign(
            tenant_id=tid,
            name="Q3 expansion",
            list_id="list1",
            icp={"industries": ["SaaS"]},
            created_by_user_id="u1",
        )
        ts.add(c)
        await ts.flush()

        assert c.status == CAMP_DRAFT_PENDING
        assert c.sequence == "ai-orchestrated-outbound"
        assert c.report == {}
        assert c.icp == {"industries": ["SaaS"]}


async def test_campaign_target_defaults():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        t = CampaignTarget(tenant_id=tid, campaign_id="c1", account_id="a1")
        ts.add(t)
        await ts.flush()

        assert t.status == TARGET_PENDING
        assert t.skip_reason is None
        assert t.draft == {}
