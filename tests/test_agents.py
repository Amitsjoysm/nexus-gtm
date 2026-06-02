"""Agent runtime: every agent runs end-to-end against the deterministic stub LLM."""
from __future__ import annotations

from nexus.agents.runtime import AgentRuntime, available_agents
from nexus.agents.llm import get_llm_provider
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore
from nexus.models.relevance import RelevanceProfile
from nexus.relevance.engine import get_relevance_engine
from tests.conftest import make_tenant, tenant_session


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
    ts.add(Contact(tenant_id=tenant_id, account_id=acc.id, full_name="Jane Doe", title="VP Sales"))
    await ts.flush()
    return acc


def _runtime() -> AgentRuntime:
    return AgentRuntime(llm=get_llm_provider(), relevance=get_relevance_engine(), browser=None)


async def test_all_agents_registered():
    assert set(available_agents()) >= {"research", "scoring", "messaging", "contact_rec", "qa"}


async def test_scoring_agent_persists_score():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        result = await _runtime().run("scoring", ts, account_id=acc.id)
        assert result.status == "completed"
        assert 0 <= result.output["composite"] <= 100
        scores = await ts.list(AccountScore)
        assert len(scores) == 1
        assert scores[0].composite == result.output["composite"]


async def test_research_and_messaging_complete():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        rt = _runtime()
        for name in ("research", "messaging", "contact_rec"):
            res = await rt.run(name, ts, account_id=acc.id)
            assert res.status == "completed", f"{name}: {res.error}"
            assert res.output


async def test_qa_requires_question_then_answers():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed(ts, tid)
        rt = _runtime()
        missing = await rt.run("qa", ts, account_id=acc.id)
        assert "error" in missing.output

        answered = await rt.run("qa", ts, account_id=acc.id, question="Why is this a fit?")
        assert answered.status == "completed"
        assert answered.output["answer"]


async def test_unknown_agent_raises():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        import pytest
        with pytest.raises(ValueError):
            await _runtime().run("does_not_exist", ts)
