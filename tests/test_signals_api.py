"""Signal library browse endpoint: listing, filtering, tenant isolation."""
from __future__ import annotations

from tests.conftest import auth, signup


async def _account_with_signals(client, h) -> str:
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software"})).json()
    ingest = await client.post(f"/api/ingest/{acc['id']}", headers=h)
    assert ingest.status_code == 200
    assert len(ingest.json()["new_signals"]) >= 1
    return acc["id"]


async def test_list_signals(client):
    h = auth(await signup(client))
    account_id = await _account_with_signals(client, h)

    r = await client.get("/api/signals", headers=h)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 1
    assert all(s["account_id"] == account_id for s in rows)
    # Newest-first ordering by occurred_at.
    occ = [s["occurred_at"] for s in rows]
    assert occ == sorted(occ, reverse=True)


async def test_filter_signals_by_kind(client):
    h = auth(await signup(client))
    await _account_with_signals(client, h)

    all_rows = (await client.get("/api/signals", headers=h)).json()
    a_kind = all_rows[0]["kind"]
    filtered = (await client.get(f"/api/signals?kind={a_kind}", headers=h)).json()
    assert filtered and all(s["kind"] == a_kind for s in filtered)


async def test_signals_are_tenant_isolated(client):
    h1 = auth(await signup(client, slug="alpha", email="a@alpha.com"))
    h2 = auth(await signup(client, slug="beta", email="b@beta.com"))
    await _account_with_signals(client, h1)

    assert (await client.get("/api/signals", headers=h2)).json() == []
