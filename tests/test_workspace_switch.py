"""Workspace creation + switching: an existing owner can spin up a second tenant and move
between them. Closes the gap where a one-tenant owner had nothing to switch to."""
from __future__ import annotations

import pytest

from nexus.core.security import decode_access_token
from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_owner_creates_second_workspace_and_switcher_lists_both(client):
    token = await signup(client, slug="acme", email="owner@acme.x", company="Acme")

    # Initially the user belongs to one tenant -> switcher would be static.
    r = await client.get("/api/auth/tenants", headers=auth(token))
    assert r.status_code == 200 and len(r.json()) == 1, r.text

    # Create a second workspace; the response is a fresh token pinned to the new tenant.
    r = await client.post(
        "/api/auth/workspaces", headers=auth(token), json={"name": "Beta Corp", "slug": "beta"}
    )
    assert r.status_code == 201, r.text
    new_token = r.json()["access_token"]
    new_tid = r.json()["access_token"] and decode_access_token(new_token)["tid"]
    assert r.json()["tenant_id"] == new_tid
    assert r.json()["role"] == "owner"

    # The user now belongs to BOTH tenants, so the switcher becomes interactive.
    r = await client.get("/api/auth/tenants", headers=auth(new_token))
    assert r.status_code == 200, r.text
    names = sorted(t["name"] for t in r.json())
    assert names == ["Acme", "Beta Corp"]
    assert all(t["role"] == "owner" for t in r.json())


@pytest.mark.asyncio
async def test_switch_between_created_workspaces_round_trips(client):
    token = await signup(client, slug="org1", email="o@org1.x", company="Org One")
    original_tid = decode_access_token(token)["tid"]
    r = await client.post(
        "/api/auth/workspaces", headers=auth(token), json={"name": "Org Two", "slug": "org2"}
    )
    second_tid = r.json()["tenant_id"]

    # Switch back to the original tenant by id.
    r = await client.post(
        "/api/auth/switch", headers=auth(r.json()["access_token"]), json={"tenant_id": original_tid}
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == original_tid

    # ...and forward to the new one again.
    r = await client.post(
        "/api/auth/switch", headers=auth(token), json={"tenant_id": second_tid}
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == second_tid


@pytest.mark.asyncio
async def test_new_workspace_starts_empty_and_isolated(client):
    """A freshly created workspace shares nothing with the user's other tenants."""
    token = await signup(client, slug="src", email="o@src.x", company="Src Co")
    # Seed an account in the first tenant.
    r = await client.post("/api/accounts", headers=auth(token), json={"name": "A", "domain": "a.x"})
    assert r.status_code == 201, r.text

    r = await client.post(
        "/api/auth/workspaces", headers=auth(token), json={"name": "Fresh", "slug": "fresh"}
    )
    fresh_token = r.json()["access_token"]
    r = await client.get("/api/accounts", headers=auth(fresh_token))
    assert r.status_code == 200, r.text
    assert r.json() == []  # no cross-tenant leak


@pytest.mark.asyncio
async def test_duplicate_workspace_slug_is_409(client):
    token = await signup(client, slug="dup", email="o@dup.x", company="Dup Co")
    body = {"name": "Another", "slug": "dup"}  # 'dup' is already this user's first tenant
    r = await client.post("/api/auth/workspaces", headers=auth(token), json=body)
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_create_workspace_requires_auth(client):
    r = await client.post("/api/auth/workspaces", json={"name": "X", "slug": "x"})
    assert r.status_code in (401, 403), r.text
