# tests/test_chat_service.py
"""ChatService: ICP seeding, the clarify→launch control loop, and save-icp (offline)."""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session


@pytest.mark.asyncio
async def test_create_session_seeds_icp_from_saved_profile():
    from nexus.orchestration.chat_service import ChatService
    from nexus.relevance.engine import get_or_create_profile

    tid = await make_tenant("t-cs")
    async with tenant_session(tid) as ts:
        prof = await get_or_create_profile(ts)
        prof.icp = {"industries": ["Fintech"], "countries": ["United States"]}
        await ts.flush()
        session, _ = await ChatService().create_session(ts, created_by=None)
        assert session.icp_state["industries"] == ["Fintech"]
        assert session.icp_state["geo"] == ["United States"]


@pytest.mark.asyncio
async def test_post_message_clarifies_then_launches_discovery():
    from nexus.models.account import Account
    from nexus.models.orchestration import OrchestrationRun
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs2")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="FinOne", domain="finone.com", industry="Fintech",
                       employee_count=800, country="United States"))
        await ts.flush()
        svc = ChatService()
        session, _ = await svc.create_session(ts, created_by=None)

        a1 = await svc.post_message(ts, session, "find fintech companies", created_by=None)
        assert a1[0].kind == "clarifying_question"          # missing geo
        await svc.post_message(ts, session, "United States", created_by=None)  # → missing size
        a3 = await svc.post_message(ts, session, "200 to 5000", created_by=None)
        assert a3[0].kind in ("text", "run_launched")       # complete → confirm
        a4 = await svc.post_message(ts, session, "go", created_by=None)
        assert a4[-1].kind == "run_launched"
        run = await ts.get(OrchestrationRun, a4[-1].data["run_id"])
        assert run is not None and run.goal == "discover"
        assert run.chat_session_id == session.id
        assert "discovery" in (run.blackboard or {})


@pytest.mark.asyncio
async def test_save_icp_persists_chat_state_to_profile():
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs3")
    async with tenant_session(tid) as ts:
        svc = ChatService()
        session, _ = await svc.create_session(ts, created_by=None)
        session.icp_state = {"industries": ["SaaS"], "geo": ["Canada"],
                             "company_size": {"min": 50, "max": 500}}
        await ts.flush()
        prof = await svc.save_icp(ts, session)
        assert prof.icp["industries"] == ["SaaS"]
        assert prof.icp["countries"] == ["Canada"]
        assert prof.icp["employee_min"] == 50 and prof.icp["employee_max"] == 500


@pytest.mark.asyncio
async def test_child_session_inherits_summary_and_icp():
    from nexus.orchestration.chat_service import ChatService

    tid = await make_tenant("t-cs4")
    async with tenant_session(tid) as ts:
        svc = ChatService()
        parent, _ = await svc.create_session(ts, created_by=None)
        parent.icp_state = {"industries": ["Fintech"]}
        parent.context_summary = "Target companies; industries: Fintech."
        await ts.flush()
        child, _ = await svc.create_session(ts, created_by=None, parent_session_id=parent.id)
        assert child.icp_state["industries"] == ["Fintech"]
        assert child.context_summary == "Target companies; industries: Fintech."
