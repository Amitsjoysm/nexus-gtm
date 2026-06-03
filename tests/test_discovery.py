# tests/test_discovery.py
"""DiscoveryAgent: own-data ICP ranking + web gap-fill (deterministic, offline)."""
from __future__ import annotations

import pytest

from tests.conftest import FakeBrowser, make_tenant, tenant_session


def _runtime(browser):
    from nexus.agents.llm import StubLLMProvider
    from nexus.agents.runtime import AgentRuntime
    from nexus.relevance import get_relevance_engine

    return AgentRuntime(llm=StubLLMProvider(), relevance=get_relevance_engine(), browser=browser)


async def _seed_accounts(ts, tid):
    from nexus.models.account import Account

    a = Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                employee_count=800, country="United States")
    b = Account(tenant_id=tid, name="HealthCo", domain="healthco.com", industry="Healthcare",
                employee_count=300, country="United States")
    c = Account(tenant_id=tid, name="FinTwo", domain="fintwo.com", industry="Fintech",
                employee_count=50, country="United States")  # too small for the band
    for acc in (a, b, c):
        ts.add(acc)
    await ts.flush()


@pytest.mark.asyncio
async def test_discovery_ranks_own_data_by_icp_fit():
    tid = await make_tenant("t-disc")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    async with tenant_session(tid) as ts:
        await _seed_accounts(ts, tid)
        rt = _runtime(FakeBrowser([]))  # no web hits → own data only
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
    out = res.output
    assert res.status == "completed"
    assert out["counts"]["new"] == 0
    names = [c["name"] for c in out["candidates"]]
    # FinOne (in-band Fintech/US) ranks above HealthCo (wrong industry) and FinTwo (too small).
    assert names[0] == "FinOne"
    assert all(c["source"] == "own" for c in out["candidates"])


@pytest.mark.asyncio
async def test_discovery_web_gapfill_creates_discovery_accounts():
    tid = await make_tenant("t-disc2")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    hits = [{"title": "NewBank", "url": "https://newbank.com/about", "domain": "newbank.com",
             "snippet": "A fintech challenger bank."}]
    async with tenant_session(tid) as ts:
        rt = _runtime(FakeBrowser(hits))  # empty own data, one web hit
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
        out = res.output
        assert out["counts"]["new"] == 1
        cand = out["candidates"][0]
        assert cand["source"] == "discovery" and cand["is_new"] is True
        # The net-new account is persisted with source="discovery".
        from nexus.models.account import Account
        acc = await ts.first(Account, Account.domain == "newbank.com")
        assert acc is not None and acc.source == "discovery"


@pytest.mark.asyncio
async def test_discovery_dedupes_web_hit_against_existing_domain():
    tid = await make_tenant("t-disc3")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    hits = [{"title": "FinOne", "url": "https://finone.com", "domain": "finone.com",
             "snippet": "x"}]
    async with tenant_session(tid) as ts:
        from nexus.models.account import Account
        ts.add(Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                       employee_count=800, country="United States"))
        await ts.flush()
        rt = _runtime(FakeBrowser(hits))
        res = await rt.run("discovery", ts, account_id=None,
                           target="companies", icp=icp, max_candidates=10)
        out = res.output
        # The hit collapses onto the existing account — no net-new row.
        assert out["counts"]["new"] == 0
        domains = [c["domain"] for c in out["candidates"]]
        assert domains.count("finone.com") == 1


@pytest.mark.asyncio
async def test_discover_goal_runs_inline_and_writes_blackboard():
    from nexus.orchestration.engine import get_orchestration_engine
    from nexus.models.orchestration import RUN_COMPLETED

    tid = await make_tenant("t-disc-goal")
    icp = {"industries": ["Fintech"], "geo": ["United States"],
           "company_size": {"min": 200, "max": 5000}}
    async with tenant_session(tid) as ts:
        await _seed_accounts(ts, tid)
        engine = get_orchestration_engine()
        run = await engine.create_run(
            ts, "discover",
            goal_input={"target": "companies", "icp": icp, "max_candidates": 10},
        )
        await engine.execute_run(ts, run)
        assert run.status == RUN_COMPLETED
        disc = run.blackboard["discovery"]
        assert disc["counts"]["own"] == 2  # FinOne + HealthCo survive (FinTwo too small)
        assert any(c["source"] == "own" for c in disc["candidates"])


def test_discover_goal_is_available_and_read_only():
    from nexus.orchestration.planner import available_goals, get_planner

    assert "discover" in available_goals()
    plan = get_planner().plan("discover", {"target": "companies", "icp": {}})
    assert len(plan) == 1
    assert plan[0]["tool"] == "discovery"
    assert plan[0]["requires_approval"] is False
