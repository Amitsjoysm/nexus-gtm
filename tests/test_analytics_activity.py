"""Live Dashboard: unified recent-activity feed (tenant-scoped, manager+)."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _seed_account_with_activity(client, token, *, name="Acme", domain="acme.x") -> str:
    """Create an account and run the full pipeline so it produces signals, a score, and runs."""
    acct = await client.post(
        "/api/accounts", headers=auth(token), json={"name": name, "domain": domain}
    )
    assert acct.status_code == 201, acct.text
    aid = acct.json()["id"]
    r = await client.post(f"/api/agents/pipeline/{aid}", headers=auth(token))
    assert r.status_code in (200, 201), r.text
    return aid


@pytest.mark.asyncio
async def test_activity_returns_recent_unified_feed(client):
    token = await signup(client, slug="act", email="o@act.x", company="ActCo")
    await _seed_account_with_activity(client, token)

    r = await client.get("/api/analytics/activity", headers=auth(token))
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list) and len(items) > 0

    # Unified across multiple sources, not just one table.
    kinds = {i["kind"] for i in items}
    assert kinds & {"signal", "account_scored", "agent_run"}

    # Every item carries the contract shape and a valid tone, ordered newest-first.
    times = []
    for i in items:
        assert i["id"] and i["kind"] and i["title"] and i["at"]
        assert i["tone"] in {"neutral", "info", "success", "warning", "critical"}
        times.append(i["at"])
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_activity_respects_limit(client):
    token = await signup(client, slug="lim", email="o@lim.x", company="LimCo")
    await _seed_account_with_activity(client, token)
    r = await client.get("/api/analytics/activity?limit=3", headers=auth(token))
    assert r.status_code == 200, r.text
    assert len(r.json()) <= 3


@pytest.mark.asyncio
async def test_activity_is_tenant_isolated(client):
    a = await signup(client, slug="iso-a", email="o@iso-a.x", company="IsoA")
    await _seed_account_with_activity(client, a, name="AlphaCo", domain="alpha.x")
    b = await signup(client, slug="iso-b", email="o@iso-b.x", company="IsoB")
    # Tenant B has provisioned no accounts, so its feed must be empty (no cross-tenant leak).
    r = await client.get("/api/analytics/activity", headers=auth(b))
    assert r.status_code == 200, r.text
    assert r.json() == []


@pytest.mark.asyncio
async def test_activity_forbidden_for_rep(client):
    owner = await signup(client, slug="actr", email="o@actr.x", company="ActrCo")
    inv = await client.post(
        "/api/workspace/members",
        headers=auth(owner),
        json={"email": "rep@actr.x", "full_name": "Rep", "password": "password123", "role": "rep"},
    )
    assert inv.status_code == 201, inv.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@actr.x", "password": "password123"}
    )
    rep = login.json()["access_token"]
    r = await client.get("/api/analytics/activity", headers=auth(rep))
    assert r.status_code == 403, r.text
