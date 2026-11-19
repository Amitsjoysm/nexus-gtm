"""The score rationale costs a model call only when something changed (owner decision 2026-11-19).

Measured on the local stack: the scoring agent ran 1,000-1,500 times a day, one per scheduled
account refresh, and asked the LLM for a two-sentence rationale every time, 75% of them for a score
identical to the account's previous one, sent to Claude first wherever an Anthropic key existed.
The call console's brief is the only screen that shows it.

* An unchanged score keeps its rationale: no call.
* The rationale goes to Groq only (`llm.GROQ_ONLY_PURPOSES`), never a paid model behind it.
* On the Free plan the refresh writes none; the call brief writes it once, when someone opens it.
"""
from __future__ import annotations

from nexus.agents import llm
from nexus.agents.llm import LLMMessage, LLMProvider, LLMResponse
from nexus.agents.runtime import AgentRuntime
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.models.relevance import RelevanceProfile
from nexus.relevance.engine import get_relevance_engine
from tests.conftest import make_tenant, tenant_session


class CountingLLM(LLMProvider):
    def __init__(self):
        self.purposes: list[str | None] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=800, purpose=None,
                       variables=None):
        self.purposes.append(purpose)
        return LLMResponse(text=f"Rationale number {len(self.purposes)}.", tokens=10)


async def _account(ts, tid) -> Account:
    ts.add(RelevanceProfile(tenant_id=tid, icp={"industries": ["Software"]},
                            value_props=[], product_context="GTM platform."))
    acc = Account(tenant_id=tid, name="Acme", domain="acme-score.example.com",
                  industry="Software", employee_count=500, country="US")
    ts.add(acc)
    await ts.flush()
    return acc


async def _plan(ts, plan_id: str) -> None:
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_plans()
    sub = await ts.first(BillingSubscription)
    if sub is None:
        ts.add(BillingSubscription(tenant_id=ts.tenant_id, plan_id=plan_id, status="active"))
    else:
        sub.plan_id = plan_id
    await ts.flush()
    ts.session.info.pop("nexus_scoring_free_plan", None)


def _runtime(fake) -> AgentRuntime:
    return AgentRuntime(llm=fake, relevance=get_relevance_engine(), browser=None)


async def test_an_unchanged_score_keeps_its_rationale_without_a_call():
    tid = await make_tenant("rat1")
    fake = CountingLLM()
    async with tenant_session(tid) as ts:
        await _plan(ts, "launch")
        acc = await _account(ts, tid)
        rt = _runtime(fake)
        first = await rt.run("scoring", ts, account_id=acc.id)
        second = await rt.run("scoring", ts, account_id=acc.id)
        third = await rt.run("scoring", ts, account_id=acc.id)

    assert fake.purposes == ["scoring_rationale"], "one call for three identical scores"
    assert first.output["rationale"] == second.output["rationale"] == third.output["rationale"]


async def test_a_changed_score_is_explained_again():
    tid = await make_tenant("rat2")
    fake = CountingLLM()
    async with tenant_session(tid) as ts:
        await _plan(ts, "launch")
        acc = await _account(ts, tid)
        rt = _runtime(fake)
        await rt.run("scoring", ts, account_id=acc.id)
        acc.employee_count = 5                         # out of the profile: the fit moves
        acc.industry = "Mining"
        await ts.flush()
        changed = await rt.run("scoring", ts, account_id=acc.id)

    assert len(fake.purposes) == 2
    assert changed.output["rationale"] == "Rationale number 2."


async def test_on_free_the_refresh_writes_none_and_the_brief_writes_it_once():
    from nexus.agents import runtime as runtime_module
    from nexus.agents.scoring import ensure_rationale

    tid = await make_tenant("rat3")
    fake = CountingLLM()
    async with tenant_session(tid) as ts:
        await _plan(ts, "free")
        acc = await _account(ts, tid)
        rt = _runtime(fake)
        result = await rt.run("scoring", ts, account_id=acc.id)
        assert result.output["rationale"] == "" and fake.purposes == []

        original = runtime_module.get_agent_runtime
        runtime_module.get_agent_runtime = lambda: rt
        try:
            score = (await ts.list(AccountScore))[-1]
            text = await ensure_rationale(ts, score)
            again = await ensure_rationale(ts, score)
        finally:
            runtime_module.get_agent_runtime = original

    assert text == again == "Rationale number 1."
    assert fake.purposes == ["scoring_rationale"], "written once, when read"


class _Named(LLMProvider):
    def __init__(self, name):
        self.name = name

    async def complete(self, messages, *, temperature=0.2, max_tokens=800, purpose=None,
                       variables=None):
        return LLMResponse(text=self.name, tokens=1)


async def test_background_purposes_go_to_groq_and_never_to_claude():
    names = ["anthropic", "groq", "openai_compat", "stub"]
    router = llm.PurposeRouter([_Named(n) for n in names[:3]] + [llm.StubLLMProvider()], names)
    for purpose in llm.GROQ_ONLY_PURPOSES:
        providers, _ = router._order(purpose)
        assert [getattr(p, "name", "stub") for p in providers] == ["groq", "stub"], purpose
        reply = await router.complete([LLMMessage("user", "x")], purpose=purpose)
        assert reply.text == "groq"
    # What a person reads as written by us keeps the deployment's order, Claude first.
    providers, _ = router._order("research_brief")
    assert getattr(providers[0], "name", "") == "anthropic"
