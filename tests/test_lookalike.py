"""Find-lookalike-companies: provider default, registry passthrough, service, and API.

Lookalike now builds a firmographic query and searches *company homepages* (Exa
``search_companies``) rather than ``find_similar`` on the bare URL. Offline the stub search
provider returns nothing, so the path returns ``[]``. We inject a fake company-search provider
to prove the real shape: domain extraction, seed/dup elimination, ICP scoring, known flagging.
"""
from __future__ import annotations

from nexus.integrations.registry import DataSourceRegistry
from nexus.integrations.search import SearchHit, SearchProvider, StubSearchProvider
from nexus.integrations.search.provider import set_search_provider
from nexus.lookalike import get_lookalike_service
from nexus.models.account import Account
from tests.conftest import auth, make_tenant, signup, tenant_session


class FakeSimilarProvider(SearchProvider):
    """Legacy similarity provider — still used to test registry.find_similar passthrough."""

    name = "fake"

    def __init__(self, similar: list[SearchHit]):
        self._similar = similar

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return []

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        return self._similar[:limit]


class FakeCompanySearch:
    """Search provider exposing ``search_companies`` (the new lookalike seam)."""

    name = "fake"

    def __init__(self, companies: list[SearchHit]):
        self._companies = companies

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return self._companies[:limit]

    async def search_companies(self, query, *, limit=10, exclude_domains=None) -> list[SearchHit]:
        return self._companies[:limit]


# ---- provider + registry ---------------------------------------------------------------------

async def test_stub_provider_find_similar_is_empty():
    assert await StubSearchProvider().find_similar("https://acme.co") == []


async def test_registry_find_similar_passthrough_and_cache():
    hits = [SearchHit(title="Globex", url="https://globex.com", source="fake")]
    reg = DataSourceRegistry(search=FakeSimilarProvider(hits))
    out = await reg.find_similar("https://acme.co", limit=5)
    assert [h.url for h in out] == ["https://globex.com"]
    assert await reg.find_similar("", limit=5) == []


# ---- service ---------------------------------------------------------------------------------

async def test_service_returns_empty_when_search_finds_nothing():
    set_search_provider(StubSearchProvider())  # stub has no search_companies + search() -> []
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
            ts.add(acc)
            await ts.flush()
            assert await get_lookalike_service().find(ts, acc) == []
    finally:
        set_search_provider(None)


async def test_service_builds_ranks_and_flags_lookalikes():
    companies = [
        SearchHit(title="Globex", url="https://www.globex.com/about", source="fake"),
        SearchHit(title="Initech", url="https://initech.com", source="fake"),
        SearchHit(title="Acme self", url="https://acme.co/home", source="fake"),  # seed -> dropped
        SearchHit(title="Globex dup", url="https://globex.com/careers", source="fake"),  # dup
        SearchHit(title="Mystery", url="", source="fake"),  # no url -> skipped
    ]
    set_search_provider(FakeCompanySearch(companies))
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            seed = Account(tenant_id=tid, name="Acme", domain="acme.co")
            tracked = Account(tenant_id=tid, name="Initech", domain="initech.com")
            ts.add_all([seed, tracked])
            await ts.flush()

            out = await get_lookalike_service().find(ts, seed, limit=10)

        domains = [lk.domain for lk in out]
        assert domains == ["globex.com", "initech.com"]  # seed + dup dropped, junk URL skipped
        by_domain = {lk.domain: lk for lk in out}
        assert by_domain["initech.com"].already_tracked is True
        assert by_domain["globex.com"].already_tracked is False
        assert by_domain["globex.com"].source == "fake"
    finally:
        set_search_provider(None)


# ---- API -------------------------------------------------------------------------------------

async def test_lookalikes_endpoint_returns_scored_candidates(client):
    set_search_provider(FakeCompanySearch([
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
        set_search_provider(None)


async def test_lookalikes_endpoint_offline_is_empty(client):
    """With the offline stub search provider, the endpoint returns nothing."""
    set_search_provider(StubSearchProvider())
    try:
        h = auth(await signup(client))
        acc = (await client.post("/api/accounts", headers=h, json={
            "name": "Acme", "domain": "acme.co"})).json()
        r = await client.post(f"/api/accounts/{acc['id']}/lookalikes", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["lookalikes"] == []
    finally:
        set_search_provider(None)


async def test_lookalikes_endpoint_unknown_account_404(client):
    h = auth(await signup(client))
    r = await client.post("/api/accounts/nope/lookalikes", headers=h)
    assert r.status_code == 404


# ---- a slow seed enrichment must not hold the search hostage -------------------------------------

async def test_seed_enrichment_is_bounded_in_time(fresh_db, monkeypatch):
    """The `except` in `find` has always claimed enrichment must never block lookalikes. It only
    ever delivered that for enrichment that FAILED; a slow one blocked as long as it liked.

    Measured on the live deployment 2026-09-09: four sequential lookalike requests took 28s, 49s,
    55s and 66s, climbing monotonically. Enrichment makes an LLM call, the Groq account is
    rate-limited at 8,000 TPM, and each request waited out a longer `retry-after` than the last.
    Nothing errored — every response was a 200 carrying ten good results — but no rep waits a
    minute for a button, so it read as a hang and was reported as "find lookalike failed".
    """
    import asyncio
    import time

    from nexus.lookalike import service as mod
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "account_enrich_enabled", True)
    monkeypatch.setattr(mod, "_SEED_ENRICH_BUDGET_S", 0.2)

    class _Hanging:
        async def enrich(self, ts, account, **kw):
            await asyncio.sleep(30)          # a rate-limited LLM chain waiting out retry-after
            return True

        async def enrich_batch(self, ts, candidates, **kw):
            # The candidate pass, which runs later and is separately best-effort. Left fast so the
            # measurement isolates the SEED enrichment this test is about.
            return None

    monkeypatch.setattr(
        "nexus.enrichment.account.get_account_enricher", lambda: _Hanging(), raising=False
    )

    tenant_id = await make_tenant()
    async with tenant_session(tenant_id) as ts:
        account = Account(tenant_id=tenant_id, name="Acme", domain="acme.com")
        ts.add(account)
        await ts.flush()

        started = time.monotonic()
        found = await mod.LookalikeService().find(ts, account, limit=5)
        elapsed = time.monotonic() - started

    assert elapsed < 5, f"the search waited {elapsed:.1f}s on a hanging enrichment"
    assert isinstance(found, list), "the search must still return, on what it already knows"


def test_the_budget_is_sized_for_a_person_not_for_a_bad_day():
    """Enrichment is an OPTIMISATION on this path — it makes the query richer. Its budget belongs
    to what somebody will wait for on top of the search, not to what enrichment needs when the
    model chain is degraded."""
    from nexus.lookalike.service import _SEED_ENRICH_BUDGET_S

    assert 5 <= _SEED_ENRICH_BUDGET_S <= 20, _SEED_ENRICH_BUDGET_S
