"""AI drafts, research and the call brief cite only signals inside the platform window.

Decided with the product owner 2026-09-23: the window applies to AI drafts and research as well as
the lists — but NOT to scoring or plays. A draft has no top bar, so it uses the superadmin's default.

The prompt already told the model how old each signal was (`_age_phrase`, written so a rep would
never open on a nine-month-old round as if it were news). The date bug defeated it: every signal
was dated at collection, so every one read "in the last few days". With true dates it works as
written, and an undated signal now says so instead of claiming to be recent.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.agents.llm import LLMProvider, LLMResponse
from nexus.agents.runtime import AgentRuntime
from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile
from nexus.models.signal import SignalEvent
from nexus.relevance.engine import get_relevance_engine
from tests.conftest import make_tenant, tenant_session

RECENT = "Acme raises $20M Series B"
OLD = "Acme raised a seed round"


class _CapturingLLM(LLMProvider):
    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=800,
                       purpose=None, variables=None) -> LLMResponse:
        self.prompts.append("\n".join(m.content for m in messages))
        return LLMResponse(
            text="Subject: Congrats on the round\n\nHi Jane,\n\nSaw the news. Worth a chat?\n\nAlex",
            tokens=10,
        )


@pytest.fixture
def monthly(monkeypatch):
    monkeypatch.setattr(get_settings(), "signal_window_default", "30")


async def _seed(ts, tid):
    ts.add(RelevanceProfile(
        tenant_id=tid, icp={"industries": ["Software"]},
        value_props=[{"name": "Faster GTM", "description": "x", "pains_solved": ["slow"]}],
        product_context="GTM platform.",
    ))
    acc = Account(tenant_id=tid, name="Acme", domain="acme.co", industry="Software")
    ts.add(acc)
    await ts.flush()
    contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", title="VP Sales",
                      phone="+15555550100")
    ts.add(contact)
    for title, age, strength in ((RECENT, 3, 0.85), (OLD, 400, 0.9)):
        # The OLD one is the stronger signal on purpose: strength ranking must not smuggle it back.
        ts.add(SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="rss",
                           title=title, dedupe_key=title, strength=strength, dated="event",
                           occurred_at=utcnow() - timedelta(days=age)))
    await ts.flush()
    return acc, contact


@pytest.mark.parametrize("agent, extra", [
    ("messaging", {}),
    ("call_script", {}),
    ("qa", {"question": "What happened at Acme recently?"}),
])
async def test_a_draft_cites_only_signals_inside_the_window(monthly, agent, extra):
    tid = await make_tenant()
    llm = _CapturingLLM()
    async with tenant_session(tid) as ts:
        acc, contact = await _seed(ts, tid)
        rt = AgentRuntime(llm=llm, relevance=get_relevance_engine(), browser=None)
        res = await rt.run(agent, ts, account_id=acc.id, contact_id=contact.id, **extra)

    assert res.status == "completed", res.error
    prompt = "\n".join(llm.prompts)
    assert RECENT in prompt, f"{agent} lost the signal inside the window"
    assert OLD not in prompt, f"{agent} cited a signal older than the window"


async def test_with_all_time_every_signal_may_be_cited(monkeypatch):
    monkeypatch.setattr(get_settings(), "signal_window_default", "all")
    tid = await make_tenant()
    llm = _CapturingLLM()
    async with tenant_session(tid) as ts:
        acc, contact = await _seed(ts, tid)
        rt = AgentRuntime(llm=llm, relevance=get_relevance_engine(), browser=None)
        await rt.run("qa", ts, account_id=acc.id, question="What happened at Acme?")

    assert OLD in "\n".join(llm.prompts)


async def test_the_call_brief_lists_only_signals_inside_the_window(monthly):
    from nexus.calling.service import get_call_queue_service
    from nexus.models.calling import CallTask

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc, contact = await _seed(ts, tid)
        task = CallTask(tenant_id=tid, account_id=acc.id, contact_id=contact.id)
        ts.add(task)
        await ts.flush()
        brief = await get_call_queue_service().build_brief(ts, task)

    titles = [s["title"] for s in brief["signals"]]
    assert RECENT in titles
    assert OLD not in titles


async def test_scoring_still_sees_every_signal(monthly):
    # The product owner kept scoring out of the window: its own 90-day decay already weighs age.
    import inspect

    from nexus.agents import scoring

    assert "within_ai_window" not in inspect.getsource(scoring)


def test_an_undated_signal_does_not_claim_to_be_recent():
    from nexus.agents.copy import signal_facts

    found = SignalEvent(kind="news", title="Acme in the news", strength=0.6,
                        occurred_at=utcnow() - timedelta(days=1), dated="found")
    old = SignalEvent(kind="funding", title="Acme raised a seed round", strength=0.9,
                      occurred_at=utcnow() - timedelta(days=400), dated="event")

    text = signal_facts([found, old])

    assert "Acme in the news" in text and "date unknown" in text
    assert "in the last few days" not in text, "an undated signal was presented as fresh"
    assert "over six months ago" in text
