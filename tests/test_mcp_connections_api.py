# tests/test_mcp_connections_api.py
"""The AI apps page's API (spec 2026-10-13, task 7). The tables have no RLS, so every endpoint is
checked against another workspace and against a colleague."""
from __future__ import annotations

import pytest

from nexus.mcp import credentials
from tests.conftest import auth, signup


@pytest.fixture(autouse=True)
def _clean():
    from nexus.core import ratelimit

    credentials.forget()
    ratelimit.reset_rate_limits()
    yield
    credentials.forget()


async def _member(client, owner, email, role="rep") -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    login = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return login.json()["access_token"]


async def _create(client, token, **kw):
    body = {"name": "Laptop", "scopes": ["read", "research", "write"], "expires_in_days": 90}
    body.update(kw)
    return await client.post("/api/connections/keys", headers=auth(token), json=body)


def _rpc(method, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


async def test_the_secret_is_returned_once_and_never_again(client):
    token = await signup(client, slug="ca1", email="o@ca1.com")
    r = await _create(client, token)
    assert r.status_code == 201, r.text
    body = r.json()
    secret = body["secret"]
    assert secret.startswith("nxk_") and body["server_url"] == "http://test/mcp"
    assert body["connection"]["key_prefix"] == secret[:12]
    listed = await client.get("/api/connections", headers=auth(token))
    assert listed.status_code == 200 and secret not in listed.text
    assert [c["name"] for c in listed.json()] == ["Laptop"]
    # And the key works.
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {secret}"},
                          json=_rpc("tools/list"))
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 18


@pytest.mark.parametrize("bad", [
    {"scopes": []}, {"scopes": ["admin"]}, {"scopes": ["read", "send"]},
    {"expires_in_days": 7}, {"name": ""}, {"surprise": True},
])
async def test_bad_key_requests_are_refused(client, bad):
    token = await signup(client, slug="ca2", email="o@ca2.com")
    assert (await _create(client, token, **bad)).status_code == 422


async def test_a_rep_sees_only_their_own_and_cannot_open_the_team_view(client):
    owner = await signup(client, slug="ca3", email="o@ca3.com")
    rep = await _member(client, owner, "rep@ca3.com")
    await _create(client, owner, name="Owner key")
    await _create(client, rep, name="Rep key")
    mine = (await client.get("/api/connections", headers=auth(rep))).json()
    assert [c["name"] for c in mine] == ["Rep key"]
    assert (await client.get("/api/connections?view=team", headers=auth(rep))).status_code == 403
    team = (await client.get("/api/connections?view=team", headers=auth(owner))).json()
    assert sorted(c["name"] for c in team) == ["Owner key", "Rep key"]
    rep_row = next(c for c in team if c["name"] == "Rep key")
    assert rep_row["owner_email"] == "rep@ca3.com" and rep_row["is_mine"] is False


async def test_a_rep_cannot_revoke_a_colleagues_key_but_an_admin_can(client):
    owner = await signup(client, slug="ca4", email="o@ca4.com")
    rep = await _member(client, owner, "rep@ca4.com")
    rep2 = await _member(client, owner, "rep2@ca4.com")
    made = (await _create(client, rep2, name="Theirs")).json()
    cid, secret = made["connection"]["id"], made["secret"]
    assert (await client.delete(f"/api/connections/{cid}", headers=auth(rep))).status_code == 404
    assert (await client.delete(f"/api/connections/{cid}",
                                headers=auth(owner))).status_code == 204
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {secret}"},
                          json=_rpc("tools/list"))
    assert r.status_code == 401


async def test_another_workspace_can_neither_see_nor_touch_a_connection(client):
    a = await signup(client, slug="ca5a", email="o@ca5a.com")
    b = await signup(client, slug="ca5b", email="o@ca5b.com")
    cid = (await _create(client, a)).json()["connection"]["id"]
    assert (await client.get("/api/connections?view=team", headers=auth(b))).json() == []
    assert (await client.delete(f"/api/connections/{cid}", headers=auth(b))).status_code == 404
    assert (await client.patch(f"/api/connections/{cid}", headers=auth(b),
                               json={"name": "x"})).status_code == 404


async def test_narrowing_permissions_changes_what_the_app_sees(client):
    token = await signup(client, slug="ca6", email="o@ca6.com")
    made = (await _create(client, token)).json()
    cid, secret = made["connection"]["id"], made["secret"]
    r = await client.patch(f"/api/connections/{cid}", headers=auth(token),
                           json={"scopes": ["read"], "name": "Read only"})
    assert r.status_code == 200 and r.json()["scopes"] == ["read"]
    tools = (await client.post("/mcp", headers={"Authorization": f"Bearer {secret}"},
                               json=_rpc("tools/list"))).json()["result"]["tools"]
    assert len(tools) == 7


async def test_an_admin_cannot_edit_someone_elses_permissions(client):
    owner = await signup(client, slug="ca7", email="o@ca7.com")
    rep = await _member(client, owner, "rep@ca7.com")
    cid = (await _create(client, rep)).json()["connection"]["id"]
    r = await client.patch(f"/api/connections/{cid}", headers=auth(owner),
                           json={"scopes": ["read", "research", "write"]})
    assert r.status_code == 404


async def test_setup_lists_the_server_and_every_tool(client):
    token = await signup(client, slug="ca8", email="o@ca8.com")
    body = (await client.get("/api/connections/setup", headers=auth(token))).json()
    assert body["server_url"] == "http://test/mcp" and body["public_address_set"] is False
    assert body["reachable_from_internet"] is False
    assert len(body["tools"]) == 18


async def test_setup_says_whether_claude_and_chatgpt_can_reach_the_address(client, monkeypatch):
    """A workspace testing on localhost followed the Claude steps and nothing connected: the
    connector is opened from Claude's servers, which cannot reach this machine. The page needs to
    know that to say so."""
    from nexus.core.config import get_settings

    token = await signup(client, slug="ca10", email="o@ca10.com")
    monkeypatch.setattr(get_settings(), "app_base_url", "https://localhost")
    body = (await client.get("/api/connections/setup", headers=auth(token))).json()
    assert body["public_address_set"] is True and body["reachable_from_internet"] is False
    monkeypatch.setattr(get_settings(), "app_base_url", "https://gtm.example.com")
    body = (await client.get("/api/connections/setup", headers=auth(token))).json()
    assert body["server_url"] == "https://gtm.example.com/mcp"
    assert body["reachable_from_internet"] is True


@pytest.mark.parametrize("url,expected", [
    ("https://gtm.infojoy.com/mcp", True),
    ("https://203.0.113.9/mcp", False),  # documentation range: reserved, never routed
    ("https://8.8.8.8/mcp", True),
    ("https://localhost/mcp", False),
    ("http://localhost:8099/mcp", False),
    ("http://127.0.0.1:9129/mcp", False),
    ("http://[::1]:8000/mcp", False),
    ("http://10.0.0.4/mcp", False),
    ("http://192.168.1.20/mcp", False),
    ("http://169.254.169.254/mcp", False),
    ("http://app.localhost/mcp", False),
    ("http://nexus.local/mcp", False),
    ("http://gtm.test/mcp", False),
    ("http://app.internal/mcp", False),
    ("http://app:8000/mcp", False),
    ("", False),
])
def test_which_addresses_the_internet_can_reach(url, expected):
    from nexus.mcp.urls import internet_reachable

    assert internet_reachable(url) is expected


async def test_creating_a_key_is_refused_while_ai_apps_are_switched_off(client):
    from nexus.core.db import get_sessionmaker
    from nexus.features import switches
    from nexus.models.feature_switch import FeatureSwitch

    token = await signup(client, slug="ca9", email="o@ca9.com")
    cid = (await _create(client, token)).json()["connection"]["id"]
    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.mcp", state="disabled", message="Paused."))
        await s.commit()
    switches.invalidate()
    try:
        r = await _create(client, token)
        assert r.status_code == 403 and "Paused" in r.text
        # Revoking is never refused.
        assert (await client.delete(f"/api/connections/{cid}",
                                    headers=auth(token))).status_code == 204
    finally:
        switches.invalidate()


async def test_revocation_and_creation_are_audited(client):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.audit import AuditLog

    token = await signup(client, slug="ca10", email="o@ca10.com")
    cid = (await _create(client, token)).json()["connection"]["id"]
    await client.delete(f"/api/connections/{cid}", headers=auth(token))
    async with get_sessionmaker()() as s:
        actions = {r.action for r in (await s.scalars(select(AuditLog))).all()}
    assert {"mcp.key_created", "mcp.connection_revoked"} <= actions
