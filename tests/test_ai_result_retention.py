"""An AI result the customer already paid for is shown again — not lost, and not re-bought.

Reported 2026-09-11: "once a brief is generated on an account it is not retained and has to be run
again; the same for the relevance engine, and wherever applicable". Measured on the running
deployment, four places threw a paid result away:

* **Account → AI Actions** (brief, draft, contact ranking, Ask). Every run IS saved — `AgentRun`
  holds the full output; Marketjoy had two completed research briefs in the table — but no endpoint
  read one back, and the page's result state started empty on every load.
* **Relevance → Draft from website.** Not recorded at all: the analysis lived in the unsaved form, so
  navigating away before "Save" discarded it and re-analysing bought it again.
* **The call console.** `generate_script` wrote `CallTask.script_cache` and nothing ever READ it;
  the console regenerated on every open.
* **The email composer.** Regenerated the draft on every open.

One hazard shapes the fix. Drafts and call scripts propose SPECIFIC DATES ("Friday 11 or Monday 14
September"). A draft reused blindly from last week would one-click-send meeting times that have
passed. So a brief, a ranking and an answer come back indefinitely, dated; a draft or a script is
reused only on the day it was written.
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import auth, signup


async def _account(client, h, name="Acme", domain="acme.com") -> str:
    r = await client.post("/api/accounts", headers=h, json={"name": name, "domain": domain})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run(tid: str, **fields) -> None:
    """A stored AgentRun, written directly: for the states a live agent cannot be made to produce."""
    from nexus.models.intelligence import AgentRun
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tid) as ts:
        ts.add(AgentRun(tenant_id=tid, **fields))
        await ts.flush()


def _tid(token: str) -> str:
    from tests.conftest import principal_from_token

    return principal_from_token(token).tenant_id


# ---- Account → AI Actions ----------------------------------------------------------------------

async def test_a_generated_brief_comes_back_when_the_page_is_opened_again(client):
    h = auth(await signup(client, slug="rt1", email="o@rt1.com", company="RT1"))
    aid = await _account(client, h)
    ran = await client.post("/api/agents/research/run", headers=h,
                            json={"account_id": aid, "inputs": {}})
    assert ran.status_code == 200 and ran.json()["status"] == "completed", ran.text

    got = await client.get("/api/agents/runs/latest", headers=h, params={"account_id": aid})
    assert got.status_code == 200, got.text
    brief = got.json()["research"]
    assert brief["output"] == ran.json()["output"]
    assert brief["created_at"], "a retained result must say when it was generated"


async def test_a_failed_run_is_never_offered_as_the_result(client):
    """A failure newer than a good brief must not replace it on screen."""
    from nexus.core.db import utcnow

    token = await signup(client, slug="rt2", email="o@rt2.com", company="RT2")
    h, tid = auth(token), _tid(token)
    aid = await _account(client, h)
    now = utcnow()
    await _run(tid, agent="research", account_id=aid, status="completed",
               output={"brief": "the good one"}, created_at=now - timedelta(hours=1))
    await _run(tid, agent="research", account_id=aid, status="failed",
               output={}, error="boom", created_at=now)

    got = (await client.get("/api/agents/runs/latest", headers=h,
                            params={"account_id": aid})).json()
    assert got["research"]["output"] == {"brief": "the good one"}


async def test_the_last_question_and_its_answer_come_back_together(client):
    token = await signup(client, slug="rt3", email="o@rt3.com", company="RT3")
    h, tid = auth(token), _tid(token)
    aid = await _account(client, h)
    await _run(tid, agent="qa", account_id=aid, status="completed",
               input={"question": "Why now?"}, output={"answer": "They just raised."})

    qa = (await client.get("/api/agents/runs/latest", headers=h,
                           params={"account_id": aid})).json()["qa"]
    assert qa["input"]["question"] == "Why now?"
    assert qa["output"]["answer"] == "They just raised."


async def test_another_workspaces_results_are_invisible(client):
    token_a = await signup(client, slug="rt4a", email="a@rt4a.com", company="RT4A")
    h_a, tid_a = auth(token_a), _tid(token_a)
    aid = await _account(client, h_a)
    await _run(tid_a, agent="research", account_id=aid, status="completed", output={"brief": "A"})

    h_b = auth(await signup(client, slug="rt4b", email="b@rt4b.com", company="RT4B"))
    got = await client.get("/api/agents/runs/latest", headers=h_b, params={"account_id": aid})
    assert got.status_code in (200, 404)
    assert got.status_code == 404 or "research" not in got.json()


# ---- per-contact drafts: reused the same day only -----------------------------------------------

async def test_a_contacts_draft_from_today_is_reused(client):
    token = await signup(client, slug="rt5", email="o@rt5.com", company="RT5")
    h, tid = auth(token), _tid(token)
    aid = await _account(client, h)
    await _run(tid, agent="messaging", account_id=aid, status="completed",
               input={"contact_id": "c1"}, output={"subject": "Hi Jane", "body": "..."})

    got = (await client.get("/api/agents/runs/latest", headers=h,
                            params={"account_id": aid, "contact_id": "c1"})).json()
    assert got["messaging"]["output"]["subject"] == "Hi Jane"
    assert got["messaging"]["fresh"] is True


async def test_one_contacts_draft_is_never_shown_for_another(client):
    token = await signup(client, slug="rt6", email="o@rt6.com", company="RT6")
    h, tid = auth(token), _tid(token)
    aid = await _account(client, h)
    await _run(tid, agent="messaging", account_id=aid, status="completed",
               input={"contact_id": "c1"}, output={"subject": "Hi Jane"})

    got = (await client.get("/api/agents/runs/latest", headers=h,
                            params={"account_id": aid, "contact_id": "c2"})).json()
    assert "messaging" not in got


async def test_yesterdays_draft_is_marked_stale(client):
    """Its proposed meeting dates may have passed — the composer regenerates rather than sends it."""
    from nexus.core.db import utcnow

    token = await signup(client, slug="rt7", email="o@rt7.com", company="RT7")
    h, tid = auth(token), _tid(token)
    aid = await _account(client, h)
    await _run(tid, agent="messaging", account_id=aid, status="completed",
               input={"contact_id": "c1"}, output={"subject": "Hi"},
               created_at=utcnow() - timedelta(days=1, hours=1))

    got = (await client.get("/api/agents/runs/latest", headers=h,
                            params={"account_id": aid, "contact_id": "c1"})).json()
    assert got["messaging"]["fresh"] is False


# ---- Relevance → Draft from website ---------------------------------------------------------------

_DRAFTED = {
    "icp": {"industries": ["Marketing Services"], "buyer_titles": ["VP Sales"]},
    "value_props": [{"name": "Qualified meetings", "description": "", "pains_solved": []}],
    "product_context": "B2B lead generation.",
}


def _analysis_returns(monkeypatch, draft: dict) -> None:
    from nexus.relevance import website_icp

    async def fake(url, **kw):
        return draft

    monkeypatch.setattr(website_icp, "analyze_website_to_icp", fake)


async def test_a_website_analysis_is_remembered(client, monkeypatch):
    h = auth(await signup(client, slug="rt8", email="o@rt8.com", company="RT8"))
    before = (await client.get("/api/relevance/last-analysis", headers=h)).json()
    assert before["url"] is None

    _analysis_returns(monkeypatch, _DRAFTED)
    r = await client.post("/api/relevance/analyze-website", headers=h,
                          json={"url": "https://marketjoy.com"})
    assert r.status_code == 200, r.text

    got = (await client.get("/api/relevance/last-analysis", headers=h)).json()
    assert got["url"] == "https://marketjoy.com"
    assert got["analyzed_at"]
    assert got["draft"] == r.json(), "the stored draft must be exactly what the analysis returned"


async def test_an_analysis_that_found_nothing_never_hides_a_good_one(client, monkeypatch):
    """A failed re-analysis (a blocked site, a rate-limited model) must not wipe the one that worked
    — that would re-create the exact "have to run it again" this retention exists to end."""
    h = auth(await signup(client, slug="rt8b", email="o@rt8b.com", company="RT8B"))
    _analysis_returns(monkeypatch, _DRAFTED)
    await client.post("/api/relevance/analyze-website", headers=h,
                      json={"url": "https://marketjoy.com"})
    _analysis_returns(monkeypatch, {"icp": {}, "value_props": [], "product_context": ""})
    await client.post("/api/relevance/analyze-website", headers=h,
                      json={"url": "https://marketjoy.com"})

    got = (await client.get("/api/relevance/last-analysis", headers=h)).json()
    assert got["draft"]["product_context"] == "B2B lead generation."


# ---- the call console --------------------------------------------------------------------------

async def _call_task(client, h) -> str:
    aid = await _account(client, h, name="Calltarget", domain="calltarget.com")
    r = await client.post("/api/calling/tasks", headers=h, json={"account_id": aid})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _counting_runtime(monkeypatch, *, script: bool = True) -> list[str]:
    """Count agent executions behind the script endpoint, each returning a real script.

    Offline, the stub LLM makes the call-script agent return an error payload with no script at all,
    which (correctly) is never cached — so a test of reuse needs an agent that actually wrote one.
    """
    from nexus.agents import runtime as runtime_mod

    real = runtime_mod.get_agent_runtime()
    calls: list[str] = []

    async def counting(agent_name, ts, **kw):
        calls.append(agent_name)
        output = ({"script": {"opener": f"Hi, take {len(calls)}", "cta": "15 minutes Friday?"}}
                  if script else {"error": "empty_completion"})
        return runtime_mod.AgentResult(agent=agent_name, status="completed", output=output)

    monkeypatch.setattr(real, "run", counting)
    return calls


async def test_reopening_a_call_reuses_todays_script(client, monkeypatch):
    h = auth(await signup(client, slug="rt9", email="o@rt9.com", company="RT9"))
    task = await _call_task(client, h)
    calls = _counting_runtime(monkeypatch)

    first = await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    second = await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    assert first.status_code == 200 and second.status_code == 200
    assert calls == ["call_script"], "the cached script was regenerated on the second open"
    assert second.json() == first.json()
    assert second.json()["generated_at"]


async def test_regenerate_writes_a_new_script(client, monkeypatch):
    h = auth(await signup(client, slug="rt10", email="o@rt10.com", company="RT10"))
    task = await _call_task(client, h)
    calls = _counting_runtime(monkeypatch)

    await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    await client.post(f"/api/calling/tasks/{task}/script", headers=h, params={"refresh": "true"})
    assert calls == ["call_script", "call_script"]


async def test_a_script_that_failed_to_generate_is_never_reused(client, monkeypatch):
    """An agent that produced no script (the stub, a rate-limited model) caches nothing, so the next
    open tries again rather than serving an empty console all day."""
    h = auth(await signup(client, slug="rt12", email="o@rt12.com", company="RT12"))
    task = await _call_task(client, h)
    calls = _counting_runtime(monkeypatch, script=False)

    await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    assert calls == ["call_script", "call_script"]


async def test_yesterdays_script_is_regenerated(client, monkeypatch):
    """A script can say "this week"; one from yesterday is regenerated rather than read out."""
    from nexus.core.db import utcnow
    from nexus.models.calling import CallTask
    from nexus.workers.tasks import tenant_session

    token = await signup(client, slug="rt11", email="o@rt11.com", company="RT11")
    h, tid = auth(token), _tid(token)
    task = await _call_task(client, h)
    async with tenant_session(tid) as ts:
        row = await ts.get(CallTask, task)
        row.script_cache = {"opener": "old", "generated_at":
                            (utcnow() - timedelta(days=1, hours=1)).isoformat()}
        await ts.flush()
    calls = _counting_runtime(monkeypatch)

    await client.post(f"/api/calling/tasks/{task}/script", headers=h)
    assert calls == ["call_script"]
