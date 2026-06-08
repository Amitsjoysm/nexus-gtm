"""Find-lookalike-companies: provider default, registry passthrough, service, and API.

Offline the stub search provider exposes no similarity search, so the whole path returns ``[]``.
We inject a fake provider whose ``find_similar`` returns fixtures to prove the real shape: domain
extraction, seed/dup elimination, ICP scoring, and known-account flagging — all with zero network.
"""
from __future__ import annotations

import pytest

from nexus.integrations.registry import DataSourceRegistry, set_registry
from nexus.integrations.search import SearchHit, SearchProvider, StubSearchProvider
from nexus.lookalike import get_lookalike_service
from nexus.models.account import Account
from tests.conftest import auth, make_tenant, signup, tenant_session


class FakeSimilarProvider(SearchProvider):
    """A search provider that only does similarity search, from a fixed fixture set."""

    name = "fake"

    def __init__(self, similar: list[SearchHit]):
        self._similar = similar

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return []

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        return self._similar[:limit]


def _registry_with(similar: list[SearchHit]) -> DataSourceRegistry:
    return DataSourceRegistry(search=FakeSimilarProvider(similar))


# ---- provider + registry ---------------------------------------------------------------------

async def test_stub_provider_find_similar_is_empty():
    assert await StubSearchProvider().find_similar("https://acme.co") == []


async def test_registry_find_similar_passthrough_and_cache():
    hits = [SearchHit(title="Globex", url="https://globex.com", source="fake")]
    reg = _registry_with(hits)
    out = await reg.find_similar("https://acme.co", limit=5)
    assert [h.url for h in out] == ["https://globex.com"]
    # Empty seed never calls a provider.
    assert await reg.find_similar("", limit=5) == []


# ---- service ---------------------------------------------------------------------------------

async def test_service_returns_empty_without_a_domain():
    set_registry(_registry_with([SearchHit(title="x", url="https://x.com")]))
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            acc = Account(tenant_id=tid, name="No Domain Co")
            ts.add(acc)
            await ts.flush()
            assert await get_lookalike_service().find(ts, acc) == []
    finally:
        set_registry(None)


async def test_service_builds_ranks_and_flags_lookalikes():
    similar = [
        SearchHit(title="Globex", url="https://www.globex.com/about", source="fake"),
        SearchHit(title="Initech", url="https://initech.com", source="fake"),
        # Same domain as the seed — must be dropped.
        SearchHit(title="Acme self", url="https://acme.co/home", source="fake"),
        # Duplicate domain — collapses to one.
        SearchHit(title="Globex dup", url="https://globex.com/careers", source="fake"),
        # No usable URL — skipped.
        SearchHit(title="Mystery", url="", source="fake"),
    ]
    set_registry(_registry_with(similar))
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            seed = Account(tenant_id=tid, name="Acme", domain="acme.co")
            tracked = Account(tenant_id=tid, name="Initech", domain="initech.com")
            ts.add_all([seed, tracked])
            await ts.flush()

            out = await get_lookalike_service().find(ts, seed, limit=10)

        domains = [lk.domain for lk in out]
        assert domains == ["globex.com", "initech.com"]  # seed + dup dropped
        assert all(lk.score == 50 for lk in out)  # no ICP profile → neutral fit
        by_domain = {lk.domain: lk for lk in out}
        assert by_domain["initech.com"].already_tracked is True
        assert by_domain["globex.com"].already_tracked is False
        assert by_domain["globex.com"].source == "fake"
    finally:
        set_registry(None)


# ---- API -------------------------------------------------------------------------------------

async def test_lookalikes_endpoint_returns_scored_candidates(client):
    set_registry(_registry_with([
        SearchHit(title="Globex", url="https://globex.com", source="fake"),
    ]))
    try:
        h = auth(await signup(client))
        acc = (await client.post("/api/accounts", headers=h, json={
            "name": "Acme", "domain": "acme.co"})).json()
        r = await client.post(f"/api/accounts/{acc['id']}/lookalikes", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["seed_domain"] == "acme.co"
        assert [lk["domain"] for lk in body["lookalikes"]] == ["globex.com"]
    finally:
        set_registry(None)


async def test_lookalikes_endpoint_offline_is_empty(client):
    """With the offline registry (stub search has no similarity), the endpoint returns nothing."""
    set_registry(DataSourceRegistry(search=StubSearchProvider()))
    try:
        h = auth(await signup(client))
        acc = (await client.post("/api/accounts", headers=h, json={
            "name": "Acme", "domain": "acme.co"})).json()
        r = await client.post(f"/api/accounts/{acc['id']}/lookalikes", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["lookalikes"] == []
    finally:
        set_registry(None)


async def test_lookalikes_endpoint_unknown_account_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/accounts/nope/lookalikes", headers=h)
    assert r.status_code == 404
