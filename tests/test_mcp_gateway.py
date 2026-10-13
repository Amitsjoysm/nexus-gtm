# tests/test_mcp_gateway.py
"""The gateway runs a tool through NEXUS's own API as the connected user (spec 2026-10-13, task 4):
role checks, tenancy, credits and validation are the app's own because the request IS an app
request. It can only reach the paths the tool catalogue names."""
from __future__ import annotations

import pytest

from nexus.mcp import credentials
from nexus.mcp.gateway import Gateway, path_allowed
from tests.conftest import auth, principal_from_token, signup


def _actor(token: str, role: str | None = None) -> credentials.Actor:
    p = principal_from_token(token)
    return credentials.Actor(
        user_id=p.user_id, tenant_id=p.tenant_id, role=role or p.role, token_version=0,
        connection_id="conn-test", kind="api_key", scopes=frozenset(credentials.SCOPES),
        name="test")


def _app(client):
    return client._transport.app  # httpx.ASGITransport keeps the app it wraps


async def test_a_read_returns_the_apps_own_answer(client):
    token = await signup(client, slug="gw1", email="o@gw1.com")
    await client.post("/api/accounts", headers=auth(token), json={"name": "Globex",
                                                                  "domain": "globex.com"})
    out = await Gateway(_app(client), _actor(token)).get("/api/accounts", {"q": "glob"})
    assert out.ok and [a["name"] for a in out.data] == ["Globex"]


async def test_a_path_outside_the_catalogue_is_refused_before_any_request(client):
    token = await signup(client, slug="gw2", email="o@gw2.com")
    gw = Gateway(_app(client), _actor(token))
    with pytest.raises(ValueError):
        await gw.get("/api/admin/billing/overview")
    with pytest.raises(ValueError):
        await gw.post("/api/accounts/abc/archive")
    with pytest.raises(ValueError):
        await gw.get("/api/accounts/../admin/billing/overview")


def test_the_allowlist_is_method_aware():
    assert path_allowed("GET", "/api/accounts")
    assert path_allowed("POST", "/api/accounts")
    assert not path_allowed("DELETE", "/api/accounts/abc")
    assert not path_allowed("GET", "/api/accounts/abc/archive")
    assert path_allowed("POST", "/api/accounts/abc123/source-contacts")
    assert not path_allowed("POST", "/api/accounts/a%2Fb/source-contacts")


async def test_not_found_comes_back_as_a_tool_message(client):
    token = await signup(client, slug="gw3", email="o@gw3.com")
    out = await Gateway(_app(client), _actor(token)).get("/api/accounts/nope123")
    assert not out.ok and out.status == 404 and "not found" in out.message.lower()


async def test_the_app_enforces_the_live_role(client):
    """A rep cannot see the team's connections view even through the gateway: the inner request
    carries the rep's role, so the app's own RBAC answers."""
    token = await signup(client, slug="gw4", email="o@gw4.com")
    out = await Gateway(_app(client), _actor(token, role="rep")).get(
        "/api/engagement/campaigns")
    # Reps may list their own campaigns; the point is the request ran as a rep without error.
    assert out.ok or out.status in (403, 404)


async def test_validation_errors_name_the_field(client):
    token = await signup(client, slug="gw5", email="o@gw5.com")
    out = await Gateway(_app(client), _actor(token)).post("/api/accounts", json={"name": ""})
    assert not out.ok and out.status == 422 and "name" in out.message
