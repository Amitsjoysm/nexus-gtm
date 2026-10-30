"""Deleting an Orchestrator / AI run (SDR round 2, 5.1).

A run is soft-deleted (`orchestration_runs.deleted_at`, migration 0076): it leaves the AI Runs list,
its detail, its results and its conversation's run card. What it did is not rewritten. Its usage
events are their own rows, so billing and the credit report still count it: deleting a run is not
a way to make a charge disappear.
"""
from __future__ import annotations

import pytest

from nexus.core.security import create_access_token
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant
from nexus.models.orchestration import (
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    RUN_AWAITING_APPROVAL,
    RUN_CANCELLED,
    Approval,
    OrchestrationRun,
)
from tests.conftest import auth, tenant_session


def _token(tid: str, user_id: str, role: str) -> str:
    return create_access_token(user_id=user_id, tenant_id=tid, role=role)


@pytest.fixture
async def workspace(fresh_db, monkeypatch):
    """A paying workspace with credits, enforcement on, so a run burns real credits."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    async with get_sessionmaker()() as s:
        t = Tenant(name="Runs", slug="runs")
        s.add(t)
        await s.flush()
        ts = TenantSession(s, t.id)
        await ensure_subscription(ts, plan_id="accelerate")
        await grant_credits(ts, 5000, kind="grant", reason="test", idempotency_key="seed")
        await s.commit()
        tid = t.id

    async with tenant_session(tid) as ts:
        from tests.conftest import seed_relevance_profile

        await seed_relevance_profile(ts)
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co", industry="Software",
                      employee_count=500, country="US")
        ts.add(acc)
        await ts.flush()
        ts.add(Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                       title="VP Sales", email="jane@acme.co"))
        await ts.flush()
        return tid, acc.id


async def _report(tid: str) -> dict:
    from nexus.billing.usage_report import credit_usage_report

    async with tenant_session(tid) as ts:
        return await credit_usage_report(ts)


def _run_line(report: dict) -> dict | None:
    return next((r for r in report["by_capability"]
                 if r["capability_id"] == "workflow.orchestration_run"), None)


async def test_a_deleted_run_leaves_every_screen_and_the_credit_report_still_counts_it(
    client, workspace,
):
    tid, account_id = workspace
    manager = _token(tid, "u-manager", "manager")

    created = await client.post(
        "/api/orchestration/runs", headers=auth(manager),
        json={"goal": "research_only", "account_id": account_id, "input": {}},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]

    before = _run_line(await _report(tid))
    assert before is not None and before["credits"] > 0, "the run must have been charged"

    deleted = await client.delete(f"/api/orchestration/runs/{run_id}", headers=auth(manager))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"id": run_id, "deleted": True}

    listed = await client.get("/api/orchestration/runs", headers=auth(manager))
    assert run_id not in {r["id"] for r in listed.json()}
    assert (await client.get(f"/api/orchestration/runs/{run_id}",
                             headers=auth(manager))).status_code == 404
    assert (await client.get(f"/api/orchestration/runs/{run_id}/results",
                             headers=auth(manager))).status_code == 404

    # The row stays, marked; the charge stays exactly as it was.
    async with tenant_session(tid) as ts:
        run = await ts.get(OrchestrationRun, run_id)
        assert run is not None and run.deleted_at is not None
    after = _run_line(await _report(tid))
    assert after == before


async def test_deleting_twice_succeeds(client, workspace):
    tid, account_id = workspace
    manager = _token(tid, "u-manager", "manager")
    async with tenant_session(tid) as ts:
        run = OrchestrationRun(tenant_id=tid, goal="research_only", status="completed",
                               account_id=account_id)
        ts.add(run)
        await ts.flush()
        run_id = run.id

    for _ in range(2):
        r = await client.delete(f"/api/orchestration/runs/{run_id}", headers=auth(manager))
        assert r.status_code == 200, r.text


async def test_the_person_who_started_it_may_delete_it_and_another_rep_may_not(client, workspace):
    tid, account_id = workspace
    async with tenant_session(tid) as ts:
        run = OrchestrationRun(tenant_id=tid, goal="research_only", status="completed",
                               account_id=account_id, created_by="u-starter")
        ts.add(run)
        await ts.flush()
        run_id = run.id

    other = await client.delete(f"/api/orchestration/runs/{run_id}",
                                headers=auth(_token(tid, "u-other", "rep")))
    assert other.status_code == 403, other.text

    own = await client.delete(f"/api/orchestration/runs/{run_id}",
                              headers=auth(_token(tid, "u-starter", "rep")))
    assert own.status_code == 200, own.text


async def test_a_run_waiting_for_approval_is_cancelled_so_nothing_it_parked_can_be_sent(
    client, workspace,
):
    from nexus.agents.llm import get_llm_provider
    from nexus.agents.runtime import AgentRuntime
    from nexus.orchestration.engine import OrchestrationEngine
    from nexus.relevance.engine import get_relevance_engine

    tid, account_id = workspace
    manager = _token(tid, "u-manager", "manager")
    async with tenant_session(tid) as ts:
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": account_id})
        await engine.execute_run(ts, run, runtime=AgentRuntime(
            llm=get_llm_provider(), relevance=get_relevance_engine(), browser=None))
        assert run.status == RUN_AWAITING_APPROVAL
        run_id = run.id

    r = await client.delete(f"/api/orchestration/runs/{run_id}", headers=auth(manager))
    assert r.status_code == 200, r.text

    async with tenant_session(tid) as ts:
        run = await ts.get(OrchestrationRun, run_id)
        assert run.status == RUN_CANCELLED
        approvals = await ts.list(Approval, Approval.run_id == run_id)
        assert approvals and all(a.status == APPROVAL_REJECTED for a in approvals)
        assert not [a for a in approvals if a.status == APPROVAL_PENDING]

    # Its approvals leave the Approvals screen with it.
    listed = await client.get("/api/orchestration/approvals", headers=auth(manager))
    assert run_id not in {a["run_id"] for a in listed.json()}

    # The worker does not resume it either.
    from nexus.workers.tasks import handle_run_orchestration

    out = await handle_run_orchestration({"tenant_id": tid, "run_id": run_id})
    assert out["error"] == "run_not_found"


async def test_another_workspace_cannot_see_or_delete_the_run(client, workspace):
    tid, account_id = workspace
    async with tenant_session(tid) as ts:
        run = OrchestrationRun(tenant_id=tid, goal="research_only", status="completed",
                               account_id=account_id)
        ts.add(run)
        await ts.flush()
        run_id = run.id

    from tests.conftest import make_tenant

    other = await make_tenant("other", "Other")
    r = await client.delete(f"/api/orchestration/runs/{run_id}",
                            headers=auth(_token(other, "u-x", "owner")))
    assert r.status_code == 404
    async with tenant_session(tid) as ts:
        assert (await ts.get(OrchestrationRun, run_id)).deleted_at is None


async def test_the_conversation_marks_the_run_card_and_keeps_its_log(client, workspace):
    from nexus.models.chat import ChatMessage, ChatSession

    tid, account_id = workspace
    manager = _token(tid, "u-manager", "manager")
    async with tenant_session(tid) as ts:
        session = ChatSession(tenant_id=tid, title="Fintechs")
        ts.add(session)
        await ts.flush()
        run = OrchestrationRun(tenant_id=tid, goal="discover", status="completed",
                               chat_session_id=session.id)
        ts.add(run)
        await ts.flush()
        ts.add(ChatMessage(tenant_id=tid, session_id=session.id, seq=1, role="assistant",
                           kind="run_launched", content="", data={"run_id": run.id}))
        await ts.flush()
        session_id, run_id = session.id, run.id

    await client.delete(f"/api/orchestration/runs/{run_id}", headers=auth(manager))

    r = await client.get(f"/api/orchestration/chat/sessions/{session_id}", headers=auth(manager))
    assert r.status_code == 200, r.text
    card = next(m for m in r.json()["messages"] if m["kind"] == "run_launched")
    assert card["data"]["run_deleted"] is True
    # The stored message is untouched: the log is append-only.
    async with tenant_session(tid) as ts:
        stored = await ts.first(ChatMessage, ChatMessage.session_id == session_id)
        assert "run_deleted" not in (stored.data or {})
