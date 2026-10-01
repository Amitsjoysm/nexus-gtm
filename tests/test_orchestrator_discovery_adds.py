"""The orchestrator's "find companies matching my ICP" adds NEW companies, like Populate does.

Reported 2026-10-01: asked to "fetch companies matching my ICP having high relevance", the run
reported 25 companies and Accounts showed nothing new. Reproduced on the local deploy: the run
returned `counts: {own: 25, new: 0}`. `DiscoveryAgent` filled its 25-slot list with the workspace's
OWN accounts first and searched for new ones only when fewer than 25 were held, so any workspace
past 25 accounts never got a single new company from the orchestrator.

Now the run lists the workspace's best own matches AND asks the same chain Populate and the daily
sweep use (shared database, then LinkedIn, then web search) for up to that many companies the
workspace does not hold. Web candidates must clear the same fit threshold; companies added are
charged `discovery.account_added` like Populate (decided with the product owner 2026-10-01).
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nexus.integrations.company_search import CompanyCandidate
from nexus.models import Account
from tests.conftest import FakeBrowser, make_tenant, tenant_session
from tests.test_prospect_chain import PagedClient, Web, _row

ICP = {"industries": ["Fintech"], "geo": ["United States"],
       "company_size": {"min": 200, "max": 5000}}


def _runtime():
    from nexus.agents.llm import StubLLMProvider
    from nexus.agents.runtime import AgentRuntime
    from nexus.relevance import get_relevance_engine

    return AgentRuntime(llm=StubLLMProvider(), relevance=get_relevance_engine(),
                        browser=FakeBrowser([]))


@pytest.fixture
def sources(monkeypatch):
    """The two places new companies come from, replaced by doubles the test can inspect."""
    from nexus.agents import discovery
    from nexus.core.config import get_settings

    web, client = Web(), PagedClient()
    monkeypatch.setattr(discovery, "_web_search", lambda ctx: web)
    monkeypatch.setattr(discovery, "_linkedin_client", lambda: client)
    monkeypatch.setattr(get_settings(), "account_enrich_enabled", False)
    return web, client


def _good(n: int) -> CompanyCandidate:
    return CompanyCandidate(name=f"Bank {n}", domain=f"bank{n}.com", industry="Fintech",
                            country="United States", employee_count=800)


async def _hold(tid, n: int) -> None:
    """Accounts the workspace already holds, COMMITTED first. The chain writes LinkedIn pages to the
    shared store on its own platform connection; SQLite allows one writer, so a tenant transaction
    still holding these inserts would lock it out (Postgres would not: different tables)."""
    async with tenant_session(tid) as ts:
        for i in range(n):
            ts.add(Account(tenant_id=tid, name=f"Own {i}", domain=f"own{i}.com",
                           industry="Fintech", country="United States", employee_count=900))


async def _count(ts) -> int:
    return int(await ts.session.scalar(
        select(func.count()).select_from(Account).where(Account.tenant_id == ts.tenant_id)))


async def test_a_workspace_holding_more_than_the_cap_still_gets_new_companies(sources):
    web, _client = sources
    web.candidates = [_good(i) for i in range(3)]
    tid = await make_tenant("t-orch-many")
    await _hold(tid, 30)
    async with tenant_session(tid) as ts:
        res = await _runtime().run("discovery", ts, account_id=None, target="companies",
                                   icp=ICP, max_candidates=25)
        out = res.output
        assert res.status == "completed", res.error
        assert out["counts"]["new"] == 3, out["counts"]
        assert await _count(ts) == 33
        new = [c for c in out["candidates"] if c["is_new"]]
        assert {c["domain"] for c in new} == {"bank0.com", "bank1.com", "bank2.com"}
        stored = await ts.first(Account, Account.domain == "bank0.com")
        assert stored.source == "discovery"
    # The best own matches are still listed beside them, as context.
    assert out["counts"]["own"] == 25


async def test_a_web_company_that_does_not_fit_is_not_added(sources):
    web, _client = sources
    web.candidates = [CompanyCandidate(name="Bakery", domain="bakery.com", industry="Food",
                                       country="United States", employee_count=800)]
    tid = await make_tenant("t-orch-fit")
    async with tenant_session(tid) as ts:
        res = await _runtime().run("discovery", ts, account_id=None, target="companies",
                                   icp=ICP, max_candidates=10)
        assert res.output["counts"]["new"] == 0
        assert res.output["discarded"].get("low_fit") == 1
        assert await _count(ts) == 0


async def test_linkedin_companies_are_added_from_the_shared_chain(sources):
    _web, client = sources
    client.pages = {1: [_row(1), _row(2)]}
    icp = {"industries": ["Software Development"], "geo": ["United Kingdom"],
           "company_size": {"min": 50, "max": 250}}
    tid = await make_tenant("t-orch-li")
    async with tenant_session(tid) as ts:
        res = await _runtime().run("discovery", ts, account_id=None, target="companies",
                                   icp=icp, max_candidates=10)
        assert res.output["counts"]["new"] == 2, res.output
        assert client.calls, "LinkedIn was never asked"
        assert await _count(ts) == 2


async def test_the_companies_added_are_charged_once_like_populate(sources, client):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingUsageEvent
    from tests.test_prospect_populate import _put_on, _workspace

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    web, _client = sources
    web.candidates = [_good(i) for i in range(2)]
    _h, tid = await _workspace(client, "orchcharge")
    await _put_on(tid, "launch", balance=200)
    async with tenant_session(tid) as ts:
        res = await _runtime().run("discovery", ts, account_id=None, target="companies",
                                   icp=ICP, max_candidates=10, run_id="run-abc")
        assert res.output["counts"]["new"] == 2
        events = await ts.list(BillingUsageEvent,
                               BillingUsageEvent.capability_id == "discovery.account_added")
    assert [e.quantity for e in events] == [2]


async def test_a_balance_that_cannot_cover_it_buys_nothing_and_says_so(sources, client,
                                                                      monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings
    from tests.test_prospect_populate import _put_on, _workspace

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    web, _client = sources
    web.candidates = [_good(1)]
    _h, tid = await _workspace(client, "orchbroke")
    await _put_on(tid, "launch", balance=0)
    async with tenant_session(tid) as ts:
        res = await _runtime().run("discovery", ts, account_id=None, target="companies",
                                   icp=ICP, max_candidates=10)
        assert res.status == "completed", res.error
        assert res.output["counts"]["new"] == 0
        assert "credit" in res.output["notes"].get("billing", "").lower()
        assert web.calls == 0, "nothing should be searched for when it cannot be paid for"
        assert await _count(ts) == 0


async def test_the_orchestrator_run_reports_what_it_added(sources):
    """End to end through the engine, the path the chat launches."""
    from nexus.models.orchestration import RUN_COMPLETED
    from nexus.orchestration.engine import get_orchestration_engine

    web, _client = sources
    web.candidates = [_good(7)]
    tid = await make_tenant("t-orch-run")
    await _hold(tid, 26)
    async with tenant_session(tid) as ts:
        engine = get_orchestration_engine()
        run = await engine.create_run(ts, "discover", goal_input={
            "target": "companies", "icp": ICP, "max_candidates": 25})
        await engine.execute_run(ts, run)
        assert run.status == RUN_COMPLETED
        assert run.blackboard["discovery"]["counts"]["new"] == 1
        assert await _count(ts) == 27


async def test_the_results_say_why_fewer_were_added(sources, client):
    """A short run explains itself: "LinkedIn had no new companies", a refused balance, the fit
    threshold. Before, a run that added nothing showed its own-account rows and no word at all."""
    from nexus.models.orchestration import OrchestrationRun
    from nexus.orchestration.engine import get_orchestration_engine
    from tests.test_prospect_populate import _workspace

    web, _client = sources
    web.candidates = [CompanyCandidate(name="Bakery", domain="bakery.com", industry="Food",
                                       country="United States", employee_count=800)]
    h, tid = await _workspace(client, "orchwhy")
    async with tenant_session(tid) as ts:
        run = await get_orchestration_engine().create_run(ts, "discover", goal_input={
            "target": "companies", "icp": ICP, "max_candidates": 5})
        await get_orchestration_engine().execute_run(ts, run)
        run_id = run.id
    r = await client.get(f"/api/orchestration/runs/{run_id}/results", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["new"] == 0 and body["counts"]["requested"] == 5
    assert "below your fit threshold" in body["shortfall"], body["shortfall"]
    async with tenant_session(tid) as ts:
        assert (await ts.get(OrchestrationRun, run_id)).status == "completed"


def test_the_results_panel_shows_the_reason():
    from pathlib import Path

    src = Path("frontend/src/components/discovery/ResultsPanel.tsx").read_text(encoding="utf-8")
    assert "data?.shortfall" in src and "No new" in src
    assert "counts.requested" in src
