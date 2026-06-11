"""GET /lists enumerates saved prospect lists (powers the campaign list picker)."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_list_saved_lists_returns_name_and_member_count(client):
    token = await signup(client, slug="lst", email="o@lst.x", company="LstCo")
    for n in ("Acme", "Beta"):
        r = await client.post(
            "/api/accounts", headers=auth(token), json={"name": n, "domain": f"{n.lower()}.x"}
        )
        assert r.status_code == 201, r.text
    built = await client.post(
        "/api/lists", headers=auth(token), json={"name": "All accounts", "filter": {}}
    )
    assert built.status_code == 201, built.text
    lid = built.json()["id"]

    r = await client.get("/api/lists", headers=auth(token))
    assert r.status_code == 200, r.text
    lists = r.json()
    row = next((l for l in lists if l["id"] == lid), None)
    assert row is not None
    assert row["name"] == "All accounts"
    assert row["accounts"] >= 2


@pytest.mark.asyncio
async def test_list_saved_lists_is_tenant_isolated(client):
    a = await signup(client, slug="la", email="o@la.x", company="LaCo")
    await client.post("/api/accounts", headers=auth(a), json={"name": "A", "domain": "a.x"})
    await client.post("/api/lists", headers=auth(a), json={"name": "A list", "filter": {}})
    b = await signup(client, slug="lb", email="o@lb.x", company="LbCo")
    r = await client.get("/api/lists", headers=auth(b))
    assert r.status_code == 200, r.text
    assert r.json() == []
