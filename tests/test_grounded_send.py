# tests/test_grounded_send.py
"""The grounded + verified send gate (Tier-1 credibility).

A cold outbound message must never leave the building unless it was grounded in retrieved
research facts and its recipient address is not known-undeliverable. These tests prove the gate
end-to-end on the offline substrate: the stub research provider supplies deterministic facts (so
drafts are genuinely grounded), the stub verifier grades syntax, and the SEP push is recorded
rather than sent. No network.
"""
from __future__ import annotations

import pytest

from nexus.agents.llm import get_llm_provider
from nexus.agents.runtime import AgentRuntime
from nexus.integrations import sep
from nexus.integrations.sep import OutreachConnector, set_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.orchestration import (
    APPROVAL_PENDING,
    Approval,
    RUN_AWAITING_APPROVAL,
    RUN_COMPLETED,
    STEP_FAILED,
)
from nexus.models.relevance import RelevanceProfile
from nexus.orchestration.engine import OrchestrationEngine
from nexus.orchestration.tools import SendMessageTool, ToolContext, ToolError
from nexus.relevance.engine import get_relevance_engine
from tests.conftest import make_tenant, tenant_session


def _runtime() -> AgentRuntime:
    return AgentRuntime(llm=get_llm_provider(), relevance=get_relevance_engine(), browser=None)


async def _seed(ts, tenant_id, *, email: str = "jane@acme.co") -> Account:
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
                   title="VP Sales", email=email))
    await ts.flush()
    return acc


@pytest.fixture(autouse=True)
def _fresh_sep():
    set_sep_connector(OutreachConnector())
    yield
    set_sep_connector(OutreachConnector())


# -- the research stub makes drafts grounded offline ---------------------------------

async def test_research_step_grounds_the_draft_offline():
    """With browser=None, grounding now comes from the research provider stub, not the web."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())

        assert run.status == RUN_AWAITING_APPROVAL
        draft = run.blackboard["draft"]
        assert draft["grounded"] is True
        assert draft["grounding"]["facts"]            # templated facts present
        # Stub verifier grades the syntactically-valid address as "unknown" (not invalid).
        assert draft["email_status"] == "unknown"


async def test_approval_payload_carries_grounding_and_verification():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())

        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]
        assert approval.payload["grounded"] is True
        assert approval.payload["grounding"]["facts"]
        assert approval.payload["email_status"] == "unknown"


# -- the gate refuses ungrounded / undeliverable sends -------------------------------

async def test_send_tool_refuses_ungrounded_draft():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        run = await OrchestrationEngine().create_run(ts, "research_account", {"account_id": acc.id})
        # Hand-craft an ungrounded draft on the blackboard.
        run.blackboard["draft"] = {"subject": "Hi", "body": "generic", "grounded": False}
        tc = ToolContext(ts=ts, runtime=_runtime(), run=run, inputs={})
        with pytest.raises(ToolError, match="ungrounded"):
            await SendMessageTool().run(tc)
        assert sep.get_sep_connector().pushed == []


async def test_send_tool_refuses_invalid_email():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # Malformed address -> stub verifier returns "invalid".
        acc = await _seed(ts, tid, email="not-an-email")
        run = await OrchestrationEngine().create_run(ts, "research_account", {"account_id": acc.id})
        contact = (await ts.list(Contact))[0]
        run.blackboard["draft"] = {
            "subject": "Hi", "body": "grounded body", "grounded": True,
            "contact_id": contact.id,
        }
        tc = ToolContext(ts=ts, runtime=_runtime(), run=run, inputs={})
        with pytest.raises(ToolError, match="undeliverable"):
            await SendMessageTool().run(tc)
        assert sep.get_sep_connector().pushed == []


async def test_full_run_sends_when_grounded_and_deliverable():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(ts, approval.id, decision="approve", runtime=_runtime())

        assert run.status == RUN_COMPLETED
        pushed = sep.get_sep_connector().pushed
        assert len(pushed) == 1 and pushed[0]["email"] == "jane@acme.co"


async def test_ungrounded_send_fails_the_step_at_runtime():
    """If grounding is somehow lost by send time, the step fails loudly (not a silent send)."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        # Reviewer edits strip grounding off the draft -> the gate must still refuse.
        await engine.decide(
            ts, approval.id, decision="approve",
            edits={"grounded": False}, runtime=_runtime(),
        )
        steps = await engine._load_steps(ts, run)
        assert steps[3].status == STEP_FAILED
        assert sep.get_sep_connector().pushed == []
