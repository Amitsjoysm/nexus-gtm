"""Offline tests for the Segment Campaign Engine (sub-project A)."""
from __future__ import annotations

import pytest

from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_DRAFT_PENDING,
    TARGET_PENDING,
)


def test_campaign_defaults():
    c = Campaign(
        tenant_id="t1",
        name="Q3 expansion",
        list_id="list1",
        icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound",
        created_by_user_id="u1",
    )
    assert c.status == CAMP_DRAFT_PENDING
    assert c.report == {} or c.report is None


def test_campaign_target_defaults():
    t = CampaignTarget(tenant_id="t1", campaign_id="c1", account_id="a1")
    assert t.status == TARGET_PENDING
    assert t.skip_reason is None
