"""Orchestration spine: planner DAG, durable run engine, approval gate, event log.

All offline — agents run against the deterministic stub LLM, and the SEP push is recorded
rather than sent. These tests assert the safety-critical invariant: a cold-outreach send
never fires until a human approves it.
"""
from __future__ import annotations

import pytest

from nexus.agents.runtime import AgentRuntime
from nexus.agents.llm import get_llm_provider
from nexus.integrations import sep
from nexus.integrations.sep import OutreachConnector, set_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.orchestration import (
    APPROVAL_PENDING,
    Approval,
    RunEvent,
    RUN_AWAITING_APPROVAL,
    RUN_COMPLETED,
    STEP_AWAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_REJECTED,
    STEP_SKIPPED,
)
from nexus.models.relevance import RelevanceProfile
from nexus.orchestration.engine import OrchestrationEngine, OrchestrationError
from nexus.orchestration.planner import PlanError, get_planner
from nexus.relevance.engine import get_relevance_engine
from tests.conftest import make_tenant, tenant_session


def _runtime() -> AgentRuntime:
    return AgentRuntime(llm=get_llm_provider(), relevance=get_relevance_engine(), browser=None)


async def _seed(ts, tenant_id) -> Account:
    ts.add(RelevanceProfile(
        tenant_id=tenant_id,
        icp={"industries": ["Software"], "employee_min": 100, "employee_max": 1000},
        value_props=[{"name": "Faster GTM", "description": "x", "pains_solved": ["slow"]}],
        product_context="GTM platform.",
    ))
    acc = Account(tenant_id=tenant_id, name="Acme", domain="acme.co",
                  industry="Software", employee_count=500, country="US")
    ts.add(acc)
    await ts.flush()
    ts.add(Contact(tenant_id=tenant_id, account_id=acc.id, full_name="Jane Doe",
                   title="VP Sales", email="jane@acme.co"))
    await ts.flush()
    return acc


@pytest.fixture(autouse=True)
def _fresh_sep():
    """Each test gets a clean recording connector so we can assert on .pushed."""
    set_sep_connector(OutreachConnector())
    yield
    set_sep_connector(OutreachConnector())


# -- planner --------------------------------------------------------------------------

def test_planner_research_account_dag():
    plan = get_planner().plan("research_account", {})
    assert [s["tool"] for s in plan] == ["research", "scoring", "compose_message", "send_message"]
    # The terminal send must carry the approval gate, inherited from the tool.
    assert plan[-1]["requires_approval"] is True
    assert plan[-1]["depends_on"] == [2]


def test_planner_unknown_goal_raises():
    with pytest.raises(PlanError):
        get_planner().plan("teleport", {})


# -- engine: parking & resume ---------------------------------------------------------

async def test_run_parks_at_approval_gate():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())

        assert run.status == RUN_AWAITING_APPROVAL
        steps = await engine._load_steps(ts, run)
        assert steps[0].status == STEP_COMPLETED  # research
        assert steps[1].status == STEP_COMPLETED  # scoring
        assert steps[2].status == STEP_COMPLETED  # compose
        assert steps[3].status == STEP_AWAITING_APPROVAL  # send parked
        approvals = await ts.list(Approval, Approval.status == APPROVAL_PENDING)
        assert len(approvals) == 1
        # Nothing went out — the send is gated.
        assert sep.get_sep_connector().pushed == []


async def test_approve_resumes_and_sends():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(ts, approval.id, decision="approve", runtime=_runtime())

        assert run.status == RUN_COMPLETED
        steps = await engine._load_steps(ts, run)
        assert steps[3].status == STEP_COMPLETED
        pushed = sep.get_sep_connector().pushed
        assert len(pushed) == 1
        assert pushed[0]["email"] == "jane@acme.co"


async def test_approve_with_edits_applies_to_send():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(
            ts, approval.id, decision="approve",
            edits={"subject": "Edited subject"}, runtime=_runtime(),
        )
        pushed = sep.get_sep_connector().pushed
        assert pushed[0]["payload"]["subject"] == "Edited subject"


async def test_reject_skips_send_and_completes():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(ts, approval.id, decision="reject", runtime=_runtime())

        assert run.status == RUN_COMPLETED  # the run is done; the send was declined
        steps = await engine._load_steps(ts, run)
        assert steps[3].status == STEP_REJECTED
        assert sep.get_sep_connector().pushed == []


async def test_double_decision_rejected():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]
        await engine.decide(ts, approval.id, decision="approve", runtime=_runtime())

        with pytest.raises(OrchestrationError):
            await engine.decide(ts, approval.id, decision="approve", runtime=_runtime())


# -- engine: idempotency & events -----------------------------------------------------

async def test_idempotent_create_returns_same_run():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        a = await engine.create_run(ts, "research_account", {"account_id": acc.id},
                                    idempotency_key="dedupe-1")
        b = await engine.create_run(ts, "research_account", {"account_id": acc.id},
                                    idempotency_key="dedupe-1")
        assert a.id == b.id


async def test_run_only_goal_completes_without_gate():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_only", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        assert run.status == RUN_COMPLETED
        assert await ts.list(Approval) == []


async def test_event_log_is_monotonic():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())

        stmt = ts.select(RunEvent, RunEvent.run_id == run.id).order_by(RunEvent.seq)
        events = list((await ts.session.scalars(stmt)).all())
        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(seqs) + 1))
        assert events[0].type == "run.created"
        assert any(e.type == "approval.requested" for e in events)
