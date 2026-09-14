"""A rep's dashboard shows their own work instead of "Role 'rep' lacks view_analytics".

`/analytics/overview` was manager-only while the dashboard every rep lands on called it
unconditionally, so a rep's first screen was an error card. Reps now get a personal overview:
their task queue and the AI work they ran, beside the shared account book they can already
browse on the Accounts page. The team surfaces (outcome attribution, the cross-team activity
feed) stay manager-only.
"""
from __future__ import annotations

from pathlib import Path

from nexus.billing.usage import record_usage
from nexus.core.security import decode_access_token
from nexus.models.workflow import InboxTask
from tests.conftest import auth, signup, tenant_session

REP_KEYS = {"accounts", "contacts", "signals", "open_tasks", "my_agent_actions",
            "avg_composite_score"}
WORKSPACE_KEYS = {"accounts", "contacts", "signals", "open_tasks", "agent_actions",
                  "agent_failures", "plays_executed", "avg_composite_score"}


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _ids(token: str) -> tuple[str, str]:
    claims = decode_access_token(token) or {}
    return claims["sub"], claims["tid"]


async def test_a_rep_gets_their_own_overview_instead_of_a_403(client):
    owner = await signup(client, slug="rd1", email="o@rd1.x", company="RD1")
    rep = await _member(client, owner, "rep@rd1.x", "rep")
    await client.post("/api/accounts", headers=auth(owner), json={"name": "Acme", "domain": "acme.rd1"})

    r = await client.get("/api/analytics/overview", headers=auth(rep))

    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == REP_KEYS
    # The shared book is the same number the rep sees on the Accounts page.
    assert data["accounts"] == 1
    # Team health metrics are a manager's question, not a rep's.
    assert "agent_failures" not in data and "plays_executed" not in data


async def test_open_tasks_are_the_reps_queue_not_other_reps_assignments(client):
    owner = await signup(client, slug="rd2", email="o@rd2.x", company="RD2")
    rep = await _member(client, owner, "rep@rd2.x", "rep")
    other = await _member(client, owner, "other@rd2.x", "rep")
    rep_id, tid = _ids(rep)
    other_id, _ = _ids(other)

    async with tenant_session(tid) as ts:
        ts.add_all([
            InboxTask(tenant_id=tid, title="mine", owner_user_id=rep_id),
            InboxTask(tenant_id=tid, title="unassigned — anyone's to work"),
            InboxTask(tenant_id=tid, title="someone else's", owner_user_id=other_id),
            InboxTask(tenant_id=tid, title="mine but done", owner_user_id=rep_id, status="done"),
        ])
        await ts.flush()

    data = (await client.get("/api/analytics/overview", headers=auth(rep))).json()
    assert data["open_tasks"] == 2          # assigned to me + unassigned

    manager_view = (await client.get("/api/analytics/overview", headers=auth(owner))).json()
    assert manager_view["open_tasks"] == 3  # the workspace count is unchanged


async def test_agent_actions_count_only_what_this_rep_ran(client):
    owner = await signup(client, slug="rd3", email="o@rd3.x", company="RD3")
    rep = await _member(client, owner, "rep@rd3.x", "rep")
    rep_id, tid = _ids(rep)
    owner_id, _ = _ids(owner)

    async with tenant_session(tid) as ts:
        for i, (cap, user, qty) in enumerate([
            ("ai.research_brief", rep_id, 1),
            ("ai.email_draft", rep_id, 1),
            ("ai.call_script", rep_id, 1),
            ("ai.call_script", rep_id, -1),      # that script failed and was refunded
            ("ai.tokens", rep_id, 900),          # a token meter, not an action
            ("enrich.contact", rep_id, 1),       # not an AI action
            ("ai.email_draft", owner_id, 1),     # somebody else's
            ("ai.scoring", None, 1),             # background work, nobody's
        ]):
            await record_usage(ts, capability_id=cap, quantity=qty, user_id=user,
                               idempotency_key=f"rd3-{i}")

    data = (await client.get("/api/analytics/overview", headers=auth(rep))).json()
    assert data["my_agent_actions"] == 2


async def test_managers_keep_the_workspace_overview(client):
    owner = await signup(client, slug="rd4", email="o@rd4.x", company="RD4")
    manager = await _member(client, owner, "mgr@rd4.x", "manager")
    for token in (owner, manager):
        r = await client.get("/api/analytics/overview", headers=auth(token))
        assert r.status_code == 200, r.text
        assert set(r.json()) == WORKSPACE_KEYS


async def test_the_team_surfaces_stay_manager_only(client):
    owner = await signup(client, slug="rd5", email="o@rd5.x", company="RD5")
    rep = await _member(client, owner, "rep@rd5.x", "rep")
    assert (await client.get("/api/analytics/activity", headers=auth(rep))).status_code == 403
    assert (await client.get("/api/outcomes/summary", headers=auth(rep))).status_code == 403


async def test_a_reps_overview_never_counts_another_workspace(client):
    a = await signup(client, slug="rd6a", email="o@rd6a.x", company="RD6A")
    rep = await _member(client, a, "rep@rd6a.x", "rep")
    b = await signup(client, slug="rd6b", email="o@rd6b.x", company="RD6B")
    await client.post("/api/accounts", headers=auth(b), json={"name": "B Co", "domain": "b.rd6"})
    _, tid_b = _ids(b)
    async with tenant_session(tid_b) as ts:
        ts.add(InboxTask(tenant_id=tid_b, title="b's task"))
        await ts.flush()

    data = (await client.get("/api/analytics/overview", headers=auth(rep))).json()
    assert data["accounts"] == 0 and data["open_tasks"] == 0


def test_the_setup_checklist_is_not_shown_to_reps():
    """It asks for ICP and plays, which a rep cannot open. Reps never saw it before only because
    their overview errored; with a working overview it would nag them forever."""
    src = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages"
           / "DashboardPage.tsx").read_text(encoding="utf-8")
    checklist = src[src.index("<ActivationChecklist") - 120: src.index("<ActivationChecklist")]
    assert "isRep" in checklist or "canViewAttribution" in checklist, (
        "the setup checklist must be gated away from reps"
    )
