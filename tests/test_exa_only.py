"""Discovery searches with Exa, strictly. Signals and the daily scan never do.

Decided with the product owner 2026-09-10, after staging produced junk: lookalikes named "Marketjoy
Competitor", contacts from another company's page, orchestrator discovery returning nothing. Every
one traced to the same design choice — each Exa feature quietly DEGRADED when Exa was unavailable:
`build_engine` swaps a keyless Exa for DuckDuckGo, a provider without `search_companies` falls back
to arbitrary web pages, and an exhausted key pool returns `[]`, which every caller reads as "no
results". A degraded answer that looks like a real one is the worst kind: nobody can tell.

So:

* Find contacts, Lookalikes, Find similar, Orchestrator discovery and Ask AI / research use Exa and
  ONLY Exa, whatever `search_provider` says. No key, or every key rejected, is a clear error —
  `SearchUnavailable`, a 503 — never a quiet substitute.
* Signals never use Exa, even with `signal_search_provider` empty (which used to fall back to the
  global Exa) or set to "exa".
* The daily scan (the scheduled account refresh) enriches WITHOUT web search: source databases and
  the Apify B2B actor only. The "Enrich from web" button keeps "actor first, then Exa".
"""
from __future__ import annotations

import inspect

import pytest

from nexus.core.config import get_settings


@pytest.fixture
def no_explicit_provider():
    from nexus.integrations.search.provider import set_search_provider

    set_search_provider(None)
    yield
    set_search_provider(None)


@pytest.fixture
def exa_keyed(monkeypatch, no_explicit_provider):
    """A deployment people use (prod), with an Exa key. Strict whatever `search_provider` says."""
    monkeypatch.setattr(get_settings(), "env", "prod")
    monkeypatch.setattr(get_settings(), "exa_api_key", "exa-test-key")
    monkeypatch.setattr(get_settings(), "exa_api_keys", "")


@pytest.fixture
def exa_unkeyed(monkeypatch, no_explicit_provider):
    """Exa chosen, no key anywhere — strict in ANY environment, so no env change is needed around the
    API client."""
    monkeypatch.setattr(get_settings(), "search_provider", "exa")
    monkeypatch.setattr(get_settings(), "exa_api_key", "")
    monkeypatch.setattr(get_settings(), "exa_api_keys", "")


# ---- the strict resolver ------------------------------------------------------------------------

async def test_without_an_exa_key_discovery_fails_loudly_instead_of_degrading(exa_unkeyed):
    """The staging failure: a keyless Exa used to become DuckDuckGo, silently."""
    from nexus.integrations.search.provider import SearchUnavailable, exa_search

    provider = exa_search()
    with pytest.raises(SearchUnavailable, match="Exa"):
        await provider.search("companies like Marketjoy")
    with pytest.raises(SearchUnavailable):
        await provider.search_companies("companies like Marketjoy")


async def test_discovery_is_exa_even_when_the_global_provider_is_repointed(exa_keyed, monkeypatch):
    from nexus.integrations.search.engines import ExaSearchProvider
    from nexus.integrations.search.provider import exa_search

    monkeypatch.setattr(get_settings(), "search_provider", "duckduckgo")
    provider = exa_search()
    assert isinstance(provider.inner, ExaSearchProvider)


async def test_a_pool_whose_every_key_was_rejected_is_an_error_not_an_empty_answer():
    """Out of credits used to read as "no lookalikes exist"."""
    from nexus.integrations.search.provider import SearchUnavailable, StrictExaSearch

    class _Exhausted:
        api_keys = ["k1"]
        last_failure = ""

        async def _refresh_keys(self):
            pass

        async def search(self, query, *, limit=5):
            self.last_failure = "every key in the 1-key pool was rejected (#0:402)"
            return []

    with pytest.raises(SearchUnavailable, match="402"):
        await StrictExaSearch(_Exhausted()).search("x")


async def test_a_quiet_market_is_still_just_empty():
    """Strict about the BACKEND, not about results: no hits with healthy keys is a real answer."""
    from nexus.integrations.search.provider import StrictExaSearch

    class _Healthy:
        api_keys = ["k1"]
        last_failure = ""

        async def _refresh_keys(self):
            pass

        async def search(self, query, *, limit=5):
            return []

    assert await StrictExaSearch(_Healthy()).search("x") == []


# ---- the five discovery features -----------------------------------------------------------------

def test_the_registry_searches_companies_and_contacts_with_exa(exa_keyed, monkeypatch):
    """Find contacts, Find similar and Orchestrator discovery all resolve through the registry."""
    from nexus.integrations.registry import build_registry_from_settings
    from nexus.integrations.search.provider import StrictExaSearch

    monkeypatch.setattr(get_settings(), "search_provider", "duckduckgo")
    monkeypatch.setattr(get_settings(), "contact_search_provider", "firecrawl")
    # The production source list. The suite pins "stub" (the shipped default), which never searches.
    monkeypatch.setattr(get_settings(), "contact_search_sources", "search")
    reg = build_registry_from_settings()

    assert isinstance(reg.search_provider, StrictExaSearch)
    backends = [getattr(p, "search_provider", None) for p in reg.contact_search_providers]
    assert any(isinstance(b, StrictExaSearch) for b in backends)
    backends = [getattr(p, "search_provider", None) for p in reg.company_search_providers]
    assert any(isinstance(b, StrictExaSearch) for b in backends)


def test_research_and_ask_search_with_exa(exa_keyed, monkeypatch):
    from nexus.integrations.search.provider import StrictExaSearch
    from nexus.research.provider import build_research_provider

    monkeypatch.setattr(get_settings(), "search_provider", "duckduckgo")
    assert isinstance(build_research_provider("search").search_provider, StrictExaSearch)


def test_lookalikes_search_with_exa():
    from nexus.lookalike import service

    src = inspect.getsource(service.LookalikeService.find)
    assert "exa_search()" in src and "get_search_provider()" not in src


def test_the_control_plane_no_longer_offers_non_exa_discovery_routes():
    """The four per-task pickers were the non-Exa routes staging went down, and two of them were
    already read by nothing. A setting that saves, reports "in effect" and changes nothing is the
    trap this catalog's rules exist to prevent — so they are gone, not merely ignored."""
    from nexus.runtime_config.catalog import CATALOG

    keys = set(CATALOG)  # keyed by setting name
    for removed in ("enrichment_search_provider", "contact_search_provider",
                    "website_icp_search_provider", "discovery_search_provider"):
        assert removed not in keys, f"{removed} is still offered in the Control plane"


def test_enrichment_searches_with_exa(exa_keyed, monkeypatch):
    """"The B2B actor first, then only Exa" — the web step never follows `search_provider`."""
    from nexus.enrichment import account as account_mod
    from nexus.integrations.search.provider import StrictExaSearch

    monkeypatch.setattr(get_settings(), "search_provider", "duckduckgo")
    monkeypatch.setattr(account_mod, "_enricher", None)
    assert isinstance(account_mod.get_account_enricher().search, StrictExaSearch)
    monkeypatch.setattr(account_mod, "_enricher", None)


# ---- signals and the daily scan never use Exa -------------------------------------------------------

@pytest.mark.parametrize("choice", ["", "exa"])
def test_signals_never_search_with_exa(no_explicit_provider, monkeypatch, choice):
    """Empty used to fall back to the GLOBAL provider, which is Exa."""
    from nexus.ingestion.sources import DorkedSearchSource

    monkeypatch.setattr(get_settings(), "search_provider", "exa")
    monkeypatch.setattr(get_settings(), "signal_search_provider", choice)
    provider = DorkedSearchSource()._provider()
    assert getattr(provider, "name", "") != "exa"


async def test_the_daily_scan_enriches_without_web_search():
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account
    from tests.conftest import make_tenant, tenant_session

    class _Recording:
        name = "recording"
        calls: list[str] = []

        async def search(self, query, *, limit=5):
            self.calls.append(query)
            return []

    search = _Recording()
    tid = await make_tenant(slug="exa-ds")
    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        await SearchBackedAccountEnricher(search, llm=None).enrich(ts, acct, web_search=False)
    assert search.calls == [], "the daily scan spent a web search"


async def test_only_the_scheduled_sweep_enriches_without_web_search(offline_services, monkeypatch):
    """The daily scan skips web search; a person adding an account or pressing Run pipeline does
    not, because "B2B actor first, then Exa" still applies to them."""
    from nexus import pipeline
    from nexus.enrichment import account as account_mod
    from nexus.models.account import Account
    from tests.conftest import make_tenant, tenant_session

    asked: list[bool] = []

    class _Recorder:
        async def enrich(self, ts, account, **kw):
            asked.append(kw.get("web_search", True))
            return []

    monkeypatch.setattr(account_mod, "get_account_enricher", lambda: _Recorder())
    monkeypatch.setattr(get_settings(), "account_enrich_enabled", True)
    tid = await make_tenant(slug="exa-sched")
    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        await pipeline.process_account(ts, acct, scheduled=True)
        await pipeline.process_account(ts, acct, scheduled=False)
    assert asked == [False, True]


def test_the_refresh_sweep_marks_its_jobs_scheduled():
    from nexus.workers import tasks

    assert "enqueue_process_account(tid, aid, scheduled=True)" in inspect.getsource(tasks)


# ---- the failure reaches the person who clicked ---------------------------------------------------

async def test_lookalikes_without_exa_answer_503_with_the_reason(client, exa_unkeyed):
    from tests.conftest import auth, signup

    h = auth(await signup(client, slug="exa1", email="o@exa1.com", company="EXA1"))
    acct = (await client.post("/api/accounts", headers=h,
                              json={"name": "Marketjoy", "domain": "marketjoy.com"})).json()
    r = await client.post(f"/api/accounts/{acct['id']}/lookalikes", headers=h)
    assert r.status_code == 503, r.text
    assert "Exa" in r.json()["detail"]


async def test_find_contacts_without_exa_answers_503_with_the_reason(client, exa_unkeyed, monkeypatch):
    from nexus.integrations import registry as registry_mod
    from tests.conftest import auth, signup

    # The production source list, and a registry built from it rather than one memoized earlier.
    # Via monkeypatch so the previous registry is RESTORED afterwards — a strict, keyless registry
    # left memoized would leak into every later test on this worker.
    monkeypatch.setattr(get_settings(), "contact_search_sources", "search")
    monkeypatch.setattr(registry_mod, "_registry", None)
    h = auth(await signup(client, slug="exa2", email="o@exa2.com", company="EXA2"))
    acct = (await client.post("/api/accounts", headers=h,
                              json={"name": "Devbay", "domain": "devbay.com"})).json()
    r = await client.post(f"/api/accounts/{acct['id']}/source-contacts", headers=h)
    assert r.status_code == 503, r.text
    assert "Exa" in r.json()["detail"]
