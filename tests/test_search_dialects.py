"""Query dialects, provider failure handling, and the Firecrawl adapter.

The thing under test is a real, measured difference between backends: a keyword engine honours
``site:``/``inurl:`` operators, and a neural one reads them as words and returns *worse* results.
Against the live Exa API, ``(site:jobs.lever.co OR site:boards.greenhouse.io) Ramp`` returned other
companies' job postings that merely mention Ramp the product, while "Ramp job openings" returned
Ramp's own careers page. A dork written in one dialect and sent to the other backend does not
error — it quietly returns the wrong thing, which is why these are pinned.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nexus.ingestion.dorks import DORKS, DORKS_BY_SLUG
from nexus.ingestion.sources import DorkedSearchSource
from nexus.models.account import Account

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


class FakeSearch:
    query_dialect = "operator"

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        return []

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90,
                            include_domains=(), exclude_domains=()):
        return await self.search(query, limit=limit)


def _account() -> Account:
    return Account(name="Acme Corp", domain="acme.com", industry="Fintech")


# ---- both dialects exist and are clean --------------------------------------------------------

def test_every_dork_has_both_dialects():
    """Writing only one form silently halves the library on whichever backend is configured, and
    nothing in the output says so — the source just returns fewer signals."""
    for dork in DORKS:
        assert dork.phrase, f"{dork.slug} has no semantic phrase"
        q = dork.render(name="Acme Corp", domain="acme.com", industry="", now=NOW,
                        dialect="semantic")
        assert "{" not in q and "}" not in q, f"{dork.slug}: {q}"


def test_the_semantic_form_carries_no_operators():
    for dork in DORKS:
        q = dork.render(name="Acme Corp", domain="acme.com", industry="", now=NOW,
                        dialect="semantic")
        for op in ("site:", "inurl:", "intitle:", " OR ", "-site:"):
            assert op not in q, f"{dork.slug} leaks {op!r} into its semantic form: {q}"


def test_domains_travel_structurally_only_for_semantic():
    """An operator backend already has them inline as site: terms; sending them twice would narrow
    an already-narrowed query to nothing. A plain backend has neither, by necessity."""
    d = DORKS_BY_SLUG["funding_press"]
    assert d.domains(domain="acme.com", dialect="operator") == ((), ())
    assert d.domains(domain="acme.com", dialect="plain") == ((), ())
    include, exclude = d.domains(domain="acme.com", dialect="semantic")
    assert "techcrunch.com" in include
    # An allowlist makes exclusions redundant, and Exa rejects some combinations of the two.
    assert exclude == ()


def test_the_account_domain_placeholder_resolves():
    include, _ = DORKS_BY_SLUG["newsroom"].domains(domain="acme.com", dialect="semantic")
    assert include == ("acme.com",)


def test_crunchbase_directory_is_not_treated_as_a_publisher():
    """The bare domain is a company directory. Including it returned "Vanta - Crunchbase Company
    Profile & Funding": a perfect name match reporting no event at all."""
    include, _ = DORKS_BY_SLUG["funding_press"].domains(domain="acme.com", dialect="semantic")
    assert "crunchbase.com" not in include
    assert "news.crunchbase.com" in include


async def test_the_source_uses_the_dialect_the_provider_declares():
    class Semantic(FakeSearch):
        query_dialect = "semantic"

    semantic = Semantic()
    await DorkedSearchSource(search=semantic, max_queries=2).fetch(_account())
    assert all("site:" not in q for q in semantic.queries), semantic.queries

    operator = FakeSearch()
    await DorkedSearchSource(search=operator, max_queries=2).fetch(_account())
    assert any("site:" in q for q in operator.queries), operator.queries

    # `plain` is the conservative default: DuckDuckGo returns ZERO results for any query carrying
    # site:, so an operator dork there is a source that silently finds nothing forever.
    class Plain(FakeSearch):
        query_dialect = "plain"

    plain = Plain()
    await DorkedSearchSource(search=plain, max_queries=2).fetch(_account())
    assert all("site:" not in q for q in plain.queries), plain.queries


# ---- provider failure -------------------------------------------------------------------------

async def test_it_stops_the_batch_when_the_provider_itself_fails():
    """Measured: DuckDuckGo's HTML endpoint starts returning 403 after roughly ten rapid queries.
    Firing the rest of the batch returns nothing and only extends the block."""
    calls: list[str] = []

    class Refusing:
        async def search_recent(self, query, *, limit=5, days=90, include_domains=(),
                                exclude_domains=()):
            calls.append(query)
            raise RuntimeError("403 Forbidden")

    await DorkedSearchSource(search=Refusing(), max_queries=4).fetch(_account())
    assert len(calls) == 1, "the batch must stop after the first provider failure"


async def test_an_older_provider_without_the_domain_kwargs_still_works():
    """Injected doubles and any provider predating the structured filters must keep working; the
    keyword dialect carries the domains inline regardless."""
    seen: list[str] = []

    class Old:
        async def search_recent(self, query, *, limit=5, days=90):
            seen.append(query)
            return []

    await DorkedSearchSource(search=Old(), max_queries=2).fetch(_account())
    assert len(seen) == 2


def test_it_claims_a_timeout_budget_matching_its_query_count():
    """The shared 8s default assumes one request. A source killed mid-run reports nothing, which
    is indistinguishable from an account that genuinely has no signals."""
    src = DorkedSearchSource(search=FakeSearch(), max_queries=4, pace_s=1.5)
    assert src.timeout_s >= 4 * 4.0 + 3 * 1.5


async def test_pacing_is_applied_between_queries_only(monkeypatch):
    import asyncio as _asyncio

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    await DorkedSearchSource(search=FakeSearch(), max_queries=3, pace_s=1.5).fetch(_account())
    assert slept == [1.5, 1.5]      # never before the first query


# ---- firecrawl ---------------------------------------------------------------------------------

def test_firecrawl_is_registered_as_an_operator_engine():
    from nexus.integrations.search.engines import FirecrawlSearchProvider
    from nexus.integrations.search.provider import build_search_provider

    assert FirecrawlSearchProvider.query_dialect == "operator"
    # A keyless selection degrades to DuckDuckGo rather than going dark.
    assert build_search_provider("firecrawl").name in ("firecrawl", "duckduckgo")


async def test_a_keyless_firecrawl_never_touches_the_network():
    from nexus.integrations.search.engines import FirecrawlSearchProvider

    assert await FirecrawlSearchProvider(api_key="").search("acme funding") == []
    assert await FirecrawlSearchProvider(api_key="").search_recent("acme funding") == []


def test_firecrawl_parses_both_api_response_shapes():
    """v1 returned ``data`` as a list, v2 as an object keyed by source. A provider that silently
    returns nothing after a version bump looks exactly like "this company has no news"."""
    from nexus.integrations.search.engines import FirecrawlSearchProvider

    fc = FirecrawlSearchProvider(api_key="k")
    row = {"title": "Acme raises $40M", "url": "https://x.com/a", "description": "Series B"}
    for payload in ({"data": [row]}, {"data": {"web": [row]}}):
        hits = fc._parse(payload, 5)
        assert len(hits) == 1
        assert hits[0].title == "Acme raises $40M"
        assert hits[0].url == "https://x.com/a"
        assert hits[0].source == "firecrawl"

    assert fc._parse({}, 5) == []
    assert fc._parse({"data": None}, 5) == []


def test_firecrawl_maps_day_windows_onto_google_time_buckets():
    """Real recency on a keyword engine — what stops "latest" from requiring an Exa key."""
    from nexus.integrations.search.engines import _tbs_for_days

    assert _tbs_for_days(1) == "qdr:d"
    assert _tbs_for_days(7) == "qdr:w"
    assert _tbs_for_days(30) == "qdr:m"
    assert _tbs_for_days(120) == "qdr:y"
    # Wider than any bucket Google offers: send none rather than approximate.
    assert _tbs_for_days(4000) == ""


async def test_exa_prefers_the_allowlist_over_exclusions():
    from nexus.integrations.search.engines import ExaSearchProvider

    exa = ExaSearchProvider(api_key="k")
    captured: dict = {}

    async def fake_post(endpoint, payload, limit):
        captured.update(payload)
        return []

    exa._post = fake_post
    await exa.search_recent(
        "q", include_domains=("techcrunch.com",), exclude_domains=("zoominfo.com",)
    )
    assert captured["includeDomains"] == ["techcrunch.com"]
    assert "excludeDomains" not in captured
    assert "startPublishedDate" in captured


async def test_exa_falls_back_to_exclusions_when_there_is_no_allowlist():
    from nexus.integrations.search.engines import ExaSearchProvider

    exa = ExaSearchProvider(api_key="k")
    captured: dict = {}

    async def fake_post(endpoint, payload, limit):
        captured.update(payload)
        return []

    exa._post = fake_post
    await exa.search_recent("q", exclude_domains=("zoominfo.com",))
    assert captured["excludeDomains"] == ["zoominfo.com"]


# ---- Firecrawl must not displace Exa -----------------------------------------------------------

def test_a_signal_only_provider_does_not_change_the_global_one(monkeypatch):
    """The guarantee: diversifying SIGNAL collection must not touch the provider the rest of the
    app uses. `find_similar` (lookalikes) and `search_companies` (company discovery, ICP
    auto-discovery) are Exa-only capabilities — losing them would present as "no results" with
    nothing in the logs."""
    from nexus.core.config import get_settings
    from nexus.integrations.search.provider import get_search_provider, set_search_provider

    settings = get_settings()
    monkeypatch.setattr(settings, "search_provider", "exa")
    monkeypatch.setattr(settings, "exa_api_key", "k")
    monkeypatch.setattr(settings, "signal_search_provider", "firecrawl")
    monkeypatch.setattr(settings, "firecrawl_api_key", "fc")
    set_search_provider(None)
    try:
        globally = get_search_provider()
        assert globally.name == "exa"
        # ...and it still has the capabilities those features branch on.
        assert hasattr(globally, "find_similar")
        assert hasattr(globally, "search_companies")

        signals = DorkedSearchSource()._provider()
        assert signals.name == "firecrawl"

        # Resolving the signal provider must not have replaced the cached global one.
        assert get_search_provider().name == "exa"
    finally:
        set_search_provider(None)


def test_signals_default_to_the_shared_provider(monkeypatch):
    """Empty means "unchanged": nobody who has not opted in sees any difference."""
    from nexus.core.config import get_settings
    from nexus.integrations.search.provider import get_search_provider, set_search_provider

    settings = get_settings()
    monkeypatch.setattr(settings, "search_provider", "exa")
    monkeypatch.setattr(settings, "exa_api_key", "k")
    monkeypatch.setattr(settings, "signal_search_provider", "")
    set_search_provider(None)
    try:
        assert DorkedSearchSource()._provider() is get_search_provider()
    finally:
        set_search_provider(None)


def test_exa_keeps_its_lookalike_capability():
    """Regression guard for the whole point of the split: Exa overrides find_similar, the base
    class returns []. If a refactor ever routed lookalikes through a keyword engine, this fails."""
    from nexus.integrations.search.engines import ExaSearchProvider
    from nexus.integrations.search.engines import FirecrawlSearchProvider
    from nexus.integrations.search.provider import SearchProvider

    assert ExaSearchProvider.find_similar is not SearchProvider.find_similar
    assert hasattr(ExaSearchProvider, "search_companies")
    # Firecrawl deliberately does NOT claim these; it is a signals backend, not an Exa replacement.
    assert FirecrawlSearchProvider.find_similar is SearchProvider.find_similar
    assert not hasattr(FirecrawlSearchProvider, "search_companies")


def test_the_default_dialect_is_the_conservative_one():
    """A backend that has not declared operator support must not be sent operators. Over-claiming
    costs every result; under-claiming costs only some precision."""
    from nexus.integrations.search.provider import (
        DuckDuckGoSearchProvider,
        SearchProvider,
        StubSearchProvider,
    )

    assert SearchProvider.query_dialect == "plain"
    assert DuckDuckGoSearchProvider.query_dialect == "plain"
    assert StubSearchProvider.query_dialect == "plain"


def test_google_backed_engines_declare_operator_support():
    from nexus.integrations.search.engines import (
        BraveSearchProvider,
        FirecrawlSearchProvider,
        SerperSearchProvider,
    )

    for cls in (BraveSearchProvider, SerperSearchProvider, FirecrawlSearchProvider):
        assert cls.query_dialect == "operator", cls.__name__


# ---- firecrawl key rotation --------------------------------------------------------------------

def test_firecrawl_accepts_a_key_pool():
    """Same semantics as Exa: a crawl issues several queries per account, so one free-tier key is
    exhausted quickly — observed live as "1-key pool + retries all rate-limited"."""
    from nexus.integrations.search.engines import FirecrawlSearchProvider

    p = FirecrawlSearchProvider(api_keys=["a", "b", "c"])
    assert p.api_keys == ["a", "b", "c"]
    assert p.api_key == "a"
    # A single key still works through the same path.
    assert FirecrawlSearchProvider(api_key="solo").api_keys == ["solo"]
    # Blanks are dropped rather than becoming an "empty key" attempt.
    assert FirecrawlSearchProvider(api_keys=["", "  "]).api_keys == []


async def test_firecrawl_rotates_to_the_next_key_on_rate_limit(monkeypatch):
    import httpx

    from nexus.integrations.search.engines import FirecrawlSearchProvider

    used: list[str] = []

    class Resp:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"data": {"web": [{"title": "t", "url": "u", "description": "d"}]}}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            used.append(headers["Authorization"].split()[-1])
            # First key is rate-limited, the second succeeds.
            return Resp(429 if len(used) == 1 else 200)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    p = FirecrawlSearchProvider(api_keys=["first", "second"])
    hits = await p.search("q")
    assert used == ["first", "second"]
    assert len(hits) == 1
    # Rotation is sticky: the working key stays current rather than flipping back each call.
    assert p.api_key == "second"


def test_the_firecrawl_pool_is_built_from_settings(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.integrations.search.engines import build_engine

    settings = get_settings()
    monkeypatch.setattr(settings, "firecrawl_api_key", "primary")
    monkeypatch.setattr(settings, "firecrawl_api_keys", "second,third")
    provider = build_engine("firecrawl", settings)
    assert provider.name == "firecrawl"
    # Primary first, then the pool, deduped.
    assert provider.api_keys == ["primary", "second", "third"]


def test_a_keyless_firecrawl_selection_degrades_to_duckduckgo(monkeypatch):
    """Going dark would be worse: search silently returning nothing looks like "no news"."""
    from nexus.core.config import get_settings
    from nexus.integrations.search.engines import build_engine

    settings = get_settings()
    monkeypatch.setattr(settings, "firecrawl_api_key", "")
    monkeypatch.setattr(settings, "firecrawl_api_keys", "")
    assert build_engine("firecrawl", settings).name == "duckduckgo"
