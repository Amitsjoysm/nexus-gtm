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
from nexus.integrations.sep import StubSEPConnector, set_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.identity import Tenant
from nexus.models.orchestration import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    Approval,
    RunEvent,
    RUN_AWAITING_APPROVAL,
    RUN_COMPLETED,
    RUN_FAILED,
    STEP_AWAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_REJECTED,
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
    set_sep_connector(StubSEPConnector())
    yield
    set_sep_connector(StubSEPConnector())


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


async def test_reject_with_reason_is_recorded():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(
            ts, approval.id, decision="reject", reason="off-ICP, wrong persona",
            runtime=_runtime(),
        )
        refreshed = await ts.get(Approval, approval.id)
        assert refreshed.edits.get("reason") == "off-ICP, wrong persona"
        steps = await engine._load_steps(ts, run)
        assert "off-ICP" in (steps[3].error or "")


async def test_redraft_regenerates_pending_draft():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        updated = await engine.redraft(
            ts, approval.id, instructions="make it two sentences, mention SOC2", runtime=_runtime(),
        )
        # Still parked for a human; payload refreshed and flagged as redrafted.
        assert updated.status == APPROVAL_PENDING
        assert updated.payload.get("redrafted") is True
        assert (updated.payload.get("body") or "").strip()
        # The blackboard draft the send tool reads is updated too.
        assert run.blackboard["draft"]["body"] == updated.payload["body"]


async def test_redraft_requires_instructions_and_pending_state():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        with pytest.raises(OrchestrationError):
            await engine.redraft(ts, approval.id, instructions="   ", runtime=_runtime())

        await engine.decide(ts, approval.id, decision="approve", runtime=_runtime())
        with pytest.raises(OrchestrationError):  # cannot redraft a decided approval
            await engine.redraft(ts, approval.id, instructions="too late", runtime=_runtime())


async def test_approve_as_draft_saves_to_mailbox_not_sends(monkeypatch):
    """delivery_mode='draft' writes to the mailbox Drafts (IMAP) and does NOT send via SEP."""
    import imaplib

    captured: dict = {}

    class _FakeIMAP:
        def __init__(self, host, port=993, ssl_context=None):
            captured["host"] = host

        def login(self, u, p):
            captured["login"] = (u, p)

        def append(self, folder, flags, date, msg):
            captured["folder"] = folder

        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {"accounts": [
            {"id": "a1", "provider": "gmail", "username": "sdr@x.com", "password": "p",
             "from_email": "sdr@x.com", "enabled": True, "default": True},
        ]}
        await ts.flush()
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(
            ts, approval.id, decision="approve", from_account="a1", delivery_mode="draft",
            runtime=_runtime(),
        )
        assert approval.status == APPROVAL_APPROVED
        assert captured.get("login") == ("sdr@x.com", "p")   # saved to the chosen mailbox Drafts
        assert captured.get("folder") == "[Gmail]/Drafts"
        assert sep.get_sep_connector().pushed == []           # NOT sent — it was drafted


async def test_approve_with_from_account_sends_via_selected_mailbox(monkeypatch):
    """Selecting a mailbox at the gate routes the SMTP send through that account's creds."""
    import nexus.integrations.email_sender as es

    captured: dict = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=30):
            captured["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            captured["login"] = (u, p)

        def send_message(self, msg):
            captured["to"] = msg["To"]
            captured["from"] = msg["From"]

    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {
            "accounts": [
                {"id": "a1", "provider": "gmail", "username": "sdr@x.com", "password": "p1",
                 "from_email": "sdr@x.com", "enabled": True, "default": True},
                {"id": "a2", "provider": "gmail", "username": "ceo@x.com", "password": "p2",
                 "from_email": "ceo@x.com", "enabled": True},
            ]
        }
        await ts.flush()
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})
        await engine.execute_run(ts, run, runtime=_runtime())
        approval = (await ts.list(Approval, Approval.status == APPROVAL_PENDING))[0]

        await engine.decide(
            ts, approval.id, decision="approve", from_account="a2", runtime=_runtime(),
        )
        assert approval.status == APPROVAL_APPROVED
        assert captured["login"] == ("ceo@x.com", "p2")  # routed to the chosen mailbox
        assert captured["to"] == "jane@acme.co"


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


def test_research_compose_threads_contact_id_into_compose_step():
    from nexus.orchestration.planner import get_planner

    plan = get_planner().plan("research_compose", {"contact_id": "c123"})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert compose["inputs"].get("contact_id") == "c123"
    # No send step in the draft-phase recipe.
    assert all(s["tool"] != "send_message" for s in plan)


def test_research_compose_omits_contact_id_when_absent():
    from nexus.orchestration.planner import get_planner

    plan = get_planner().plan("research_compose", {})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert "contact_id" not in compose["inputs"]


async def test_runaway_guard_fails_run_instead_of_hanging(monkeypatch):
    """Regression (M-2): if a step never leaves PENDING (a hypothetical bug), _next_runnable would
    return it forever. The iteration cap must fail the run cleanly rather than wedge the worker."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        engine = OrchestrationEngine()
        run = await engine.create_run(ts, "research_account", {"account_id": acc.id})

        # Simulate a stuck step: report success but never transition it out of PENDING.
        async def _stuck_run_step(self, ts_, run_, step_, *, runtime):  # noqa: ANN001
            return True

        monkeypatch.setattr(OrchestrationEngine, "_run_step", _stuck_run_step, raising=True)

        # Must return (not hang) and end failed with the runaway marker.
        await engine.execute_run(ts, run, runtime=_runtime())
        assert run.status == RUN_FAILED
        assert run.error and "runaway_guard" in run.error


# ---- the runs list has to report progress it can actually see -----------------------------------


async def test_the_runs_list_reports_step_counts(client):
    """A finished discovery run displayed "0/0 steps" on the AI Runs list.

    `RunOut.from_model` defaults `steps` to empty, and the LIST endpoint never passed any — loading
    every step of every run, each carrying its own `output` blob, to render one "3/5" label is a
    lot of payload for a number. So the counts are aggregated separately and sent as plain ints.
    """
    from tests.conftest import auth, signup

    token = await signup(client, slug="runcnt", email="o@runcnt.com", company="RUNCNT")
    created = await client.post(
        "/api/orchestration/runs",
        headers=auth(token),
        json={"goal": "discover", "input": {"target": "companies", "max_candidates": 1}},
    )
    assert created.status_code in (200, 201), created.text

    listed = await client.get("/api/orchestration/runs", headers=auth(token))
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert rows, "the run we just created should be listed"
    row = rows[0]
    assert "step_total" in row and "step_done" in row
    assert row["step_total"] >= 1, "a discovery run has at least one step — 0 was the bug"


async def test_the_detail_view_derives_the_same_counts_from_its_steps(client):
    """When steps ARE loaded the counts come from them, so list and detail cannot disagree."""
    from tests.conftest import auth, signup

    token = await signup(client, slug="runcnt2", email="o@runcnt2.com", company="RUNCNT2")
    created = await client.post(
        "/api/orchestration/runs",
        headers=auth(token),
        json={"goal": "discover", "input": {"target": "companies", "max_candidates": 1}},
    )
    run_id = created.json()["id"]

    detail = (await client.get(f"/api/orchestration/runs/{run_id}", headers=auth(token))).json()
    assert detail["step_total"] == len(detail["steps"])
    assert detail["step_done"] == sum(1 for s in detail["steps"] if s["status"] == "completed")

    listed = (await client.get("/api/orchestration/runs", headers=auth(token))).json()
    row = next(r for r in listed if r["id"] == run_id)
    assert row["step_total"] == detail["step_total"]
