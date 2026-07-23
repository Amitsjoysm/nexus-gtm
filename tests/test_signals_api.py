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


async def test_filter_signals_by_max_age_days(client):
    """The recency window keeps fresh signals and drops old ones, server-side."""
    from datetime import timedelta

    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.signal import SignalEvent

    h = auth(await signup(client))
    account_id = await _account_with_signals(client, h)
    fresh = (await client.get("/api/signals", headers=h)).json()
    assert fresh, "ingest should have produced signals"
    tenant_id = None

    # Backdate one extra signal to 45 days ago, directly in the DB.
    async with get_sessionmaker()() as session:
        row = await session.get(SignalEvent, fresh[0]["id"])
        tenant_id = row.tenant_id
        session.add(SignalEvent(
            tenant_id=tenant_id, account_id=account_id, kind="news", source="test",
            title="Old news", dedupe_key="old-news-45d", strength=0.6,
            occurred_at=utcnow() - timedelta(days=45),
        ))
        await session.commit()

    all_rows = (await client.get("/api/signals", headers=h)).json()
    assert any(s["title"] == "Old news" for s in all_rows)  # unfiltered includes it

    windowed = (await client.get("/api/signals?max_age_days=30", headers=h)).json()
    assert windowed, "recent signals must survive the window"
    assert all(s["title"] != "Old news" for s in windowed)  # 45d-old signal excluded

    wide = (await client.get("/api/signals?max_age_days=90", headers=h)).json()
    assert any(s["title"] == "Old news" for s in wide)  # 90d window includes it

    # Bounds are validated.
    assert (await client.get("/api/signals?max_age_days=0", headers=h)).status_code == 422
    assert (await client.get("/api/signals?max_age_days=400", headers=h)).status_code == 422
