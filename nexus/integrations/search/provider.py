# nexus/integrations/search/provider.py
"""General web-search capability behind one interface.

A :class:`SearchProvider` turns a free-text query into a list of normalized
:class:`SearchHit` rows. It is the lowest-level "look something up on the web" seam; richer
capabilities (company discovery, research) compose it. Adapters NEVER raise across the boundary:
on any failure they return ``[]`` so callers degrade gracefully and offline/CI stays deterministic.

Shipped adapters:
  * :class:`StubSearchProvider` — returns ``[]``. The zero-network default for tests/CI.
  * :class:`DuckDuckGoSearchProvider` — wraps the existing :class:`BrowserProvider` (DuckDuckGo /
    Scrapling / Cloak). Exa, Brave, Serper and Tavily adapters land here later.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass

logger = logging.getLogger("nexus.integrations.search")


@dataclass(slots=True)
class SearchHit:
    """One normalized web result. ``source`` records which provider produced it."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "source": self.source}


class SearchProvider(abc.ABC):
    name: str

    #: Which query dialect this backend actually understands. Measured against the live services,
    #: not inferred from documentation — all three behaviours were observed.
    #:
    #: ``operator`` — a real Google-style SERP. Honours ``site:``, ``inurl:``, ``OR``, ``-site:``.
    #: ``plain``    — keyword matching, operators unreliable. DuckDuckGo's HTML endpoint returns
    #:   **zero results for any query containing ``site:`` or ``-site:``**, while the same query
    #:   without them returns the right pages. It does not error; it just matches nothing, so an
    #:   operator dork there is a source that silently finds nothing forever.
    #: ``semantic`` — neural retrieval. Operators are read as literal words and make results
    #:   *worse*: on Exa, ``(site:jobs.lever.co) Ramp`` returned other companies' job posts that
    #:   merely mention Ramp the product, while "Ramp job openings" returned Ramp's own careers
    #:   page. Domain filtering is a structured parameter here, not query text.
    #:
    #: ``plain`` is the conservative default: a backend that has not declared operator support gets
    #: the query form that works everywhere. Over-claiming costs every result; under-claiming costs
    #: only some precision.
    query_dialect: str = "plain"

    #: Why the LAST search could not run, empty when it could.
    #:
    #: A provider that has condemned its whole key pool still returns ``[]``, which every caller
    #: reads as "no results" — so a dead credential looks exactly like a quiet market, and the
    #: crawl history records `empty` forever with the real reason living only in a log line.
    #: Callers that care read this after a search; callers that do not are unaffected, which is
    #: why it is an attribute rather than an exception: provider isolation is the rule here, and a
    #: search backend must never raise across the boundary into the crawl that called it.
    last_failure: str = ""

    @abc.abstractmethod
    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]: ...

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        """Find pages similar to ``url`` (powers the find-lookalike play).

        Optional capability: the default returns ``[]`` so providers that can't do
        similarity search (DuckDuckGo, stub) degrade quietly and offline/CI stays
        deterministic. Adapters that can (Exa) override this.
        """
        return []

    async def search_recent(
        self,
        query: str,
        *,
        limit: int = 5,
        days: int = 90,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchHit]:
        """Search, preferring results published within ``days`` and within ``include_domains``.

        Optional capability, same pattern as :meth:`find_similar` — but the default **delegates to
        search** rather than returning ``[]``. Recency is a preference, not a requirement: a caller
        asking for recent funding news still wants results from an engine that cannot filter by
        date, and returning nothing would make the dork library useless on the keyless default.

        The domain arguments are likewise ignored here, and correctly so: a ``keyword`` backend has
        already received them as ``site:`` terms inside ``query``. They exist for ``semantic``
        backends, where domain filtering is a structured parameter and putting it in the query text
        actively degrades the results.
        """
        return await self.search(query, limit=limit)


class StubSearchProvider(SearchProvider):
    """Deterministic offline default: no network, no results."""

    name = "stub"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return []


class DuckDuckGoSearchProvider(SearchProvider):
    """Web search via the shared :class:`BrowserProvider` (DuckDuckGo by default).

    The browser layer already degrades to ``[]`` on network / anti-bot failure; we add one more
    guard so a misbehaving provider can never break the registry waterfall.
    """

    name = "duckduckgo"
    # Measured: the HTML endpoint returns ZERO results for any query containing site: or -site:,
    # while the same query without them returns the right pages. It also 403s after roughly ten
    # rapid requests. Operator dorks here are a source that silently finds nothing.
    query_dialect = "plain"

    def __init__(self, browser=None):
        # Defer resolving the global browser singleton until first use so tests can inject one.
        self._browser = browser

    def _resolve_browser(self):
        if self._browser is None:
            from nexus.enrichment.browser import get_browser_provider

            self._browser = get_browser_provider()
        return self._browser

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        try:
            hits = await self._resolve_browser().search(query, limit=limit)
        except Exception as exc:  # provider isolation — never break the caller
            logger.warning("search provider %s failed: %r", self.name, exc)
            return []
        out: list[SearchHit] = []
        for h in hits or []:
            out.append(
                SearchHit(
                    title=(h.get("title") or "").strip(),
                    url=(h.get("url") or "").strip(),
                    snippet=(h.get("snippet") or "").strip(),
                    source=self.name,
                )
            )
        return out


_search: SearchProvider | None = None
#: True when `_search` was installed by `set_search_provider` — the test seam — rather than memoized
#: from settings. Explicit installation wins over the strict Exa resolver below, exactly as
#: `set_alert_channels` wins over a tenant's stored connections: without the distinction the
#: resolver would either ignore the seam or mistake the settings singleton for an override.
_search_explicit = False


class SearchUnavailable(RuntimeError):
    """A discovery feature's search backend is unusable, and saying so beats answering badly.

    Deliberately NOT swallowed by the "provider isolation" catch-alls on the discovery paths: those
    exist so a flaky request cannot take a feature down, and they are exactly how staging turned
    "Exa is not configured" into lookalikes named "Marketjoy Competitor" and contacts from another
    company's page. Surfaced as a 503 whose message tells an operator what to fix.
    """


_EXA_NOT_CONFIGURED = (
    "Exa search is not configured, so this cannot run. A platform admin needs to add an Exa API key "
    "under Control plane -> Provider keys."
)


class StrictExaSearch(SearchProvider):
    """Exa, and only Exa, for the discovery features — failing loudly instead of degrading.

    Decided with the product owner 2026-09-10: Find contacts, Lookalikes, Find similar, Orchestrator
    discovery and Ask AI / research use Exa strictly. Each used to degrade quietly — a keyless Exa
    became DuckDuckGo, a provider without `search_companies` searched arbitrary web pages, and an
    exhausted pool returned `[]` — and a degraded answer that LOOKS like a real one is the worst
    kind, because nobody can tell. Two failures are now errors:

    * **no key anywhere.** Checked at CALL time, after loading the managed pool, so a key added in
      the Control plane works without a restart. That also fixes a latent bug: `search()` returned
      `[]` whenever no ENVIRONMENT key existed, before `_post` ever loaded the panel's keys.
    * **every key rejected** (401/402/403). `ExaSearchProvider` already records this in
      `last_failure` without raising, deliberately, so the crawl can carry on; here it becomes the
      error it is. An empty result with healthy keys is still a real answer — a quiet market.

    A thin wrapper rather than a change to `ExaSearchProvider`, whose non-raising contract other
    callers (best-effort enrichment) rely on.
    """

    name = "exa"
    query_dialect = "semantic"

    def __init__(self, inner) -> None:
        self.inner = inner

    async def _ready(self) -> None:
        refresh = getattr(self.inner, "_refresh_keys", None)
        if refresh is not None:
            await refresh()
        if not getattr(self.inner, "api_keys", None):
            raise SearchUnavailable(_EXA_NOT_CONFIGURED)

    def _checked(self, hits: list[SearchHit]) -> list[SearchHit]:
        failure = getattr(self.inner, "last_failure", "")
        if not hits and failure:
            raise SearchUnavailable(
                f"Exa search is unavailable: {failure}. Check the Exa keys under Control plane -> "
                "Provider keys (402 means the account is out of credits)."
            )
        return hits

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        await self._ready()
        return self._checked(await self.inner.search(query, limit=limit))

    async def search_companies(self, query, *, limit=10, exclude_domains=None) -> list[SearchHit]:
        await self._ready()
        return self._checked(
            await self.inner.search_companies(query, limit=limit, exclude_domains=exclude_domains)
        )

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        await self._ready()
        return self._checked(await self.inner.find_similar(url, limit=limit))

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(),
                            exclude_domains=()) -> list[SearchHit]:
        await self._ready()
        return self._checked(await self.inner.search_recent(
            query, limit=limit, days=days,
            include_domains=include_domains, exclude_domains=exclude_domains,
        ))


_exa: StrictExaSearch | None = None


def explicit_search_provider() -> SearchProvider | None:
    """The provider a test installed with `set_search_provider`, or None. Every resolver honours it."""
    return _search if _search_explicit else None


def exa_search(*, browser=None) -> SearchProvider:
    """The search backend for the discovery features: strictly Exa, whatever `search_provider` says.

    In **staging and prod** this is always `StrictExaSearch` — the setting is not consulted, because
    `search_provider` defaults to "duckduckgo" and a deployment that never set it ran every discovery
    feature on DuckDuckGo: the most likely cause of staging's junk. The local docker deploy runs as
    `env="prod"`, so it gets exactly what staging and production get.

    In **local / test** the configured provider is honoured unless it is "exa": that is where the
    offline doubles live (the stub, and DuckDuckGo over an injected test browser), and the suite has
    no Exa key. Strictness is a statement about deployments people use, not about the test harness.

    An explicitly installed provider (`set_search_provider`) wins everywhere. One shared strict
    instance, so key rotation stays sticky across calls as it is inside one `ExaSearchProvider`.
    """
    global _exa
    if _search_explicit and _search is not None:
        return _search
    from nexus.core.config import get_settings

    s = get_settings()
    configured = (s.search_provider or "").strip().lower()
    if s.env not in ("staging", "prod") and configured != "exa":
        return build_search_provider(s.search_provider, browser=browser)
    if _exa is None:
        from nexus.core.config import get_settings
        from nexus.integrations.search.engines import ExaSearchProvider

        _exa = StrictExaSearch(ExaSearchProvider(api_keys=get_settings().exa_api_key_list))
    return _exa


def signal_search_choice() -> str:
    """Which backend the signal pipeline searches with. Never Exa.

    Decided with the product owner 2026-09-10: signals and the daily scan do not use Exa. Empty used
    to mean "whatever the rest of the app uses" — the global provider, which is Exa — so leaving the
    setting blank quietly spent Exa credits on every dork. Empty and "exa" both resolve to Firecrawl,
    which `build_engine` degrades to keyless DuckDuckGo when it has no key.
    """
    from nexus.core.config import get_settings

    choice = (get_settings().signal_search_provider or "").strip().lower()
    return "firecrawl" if choice in ("", "exa") else choice


def build_search_provider(name: str, *, browser=None) -> SearchProvider:
    """Resolve a single search provider by settings token.

    ``stub``/``duckduckgo`` are keyless and built here. The hosted engines
    (``exa``/``brave``/``serper``) are delegated to :mod:`engines`, which reads their API key
    from settings and degrades a keyless selection to DuckDuckGo.
    """
    key = (name or "").strip().lower()
    if key in ("stub", "", "none"):
        return StubSearchProvider()
    if key in ("duckduckgo", "ddg"):
        return DuckDuckGoSearchProvider(browser=browser)
    if key in ("exa", "brave", "serper", "firecrawl"):
        from nexus.core.config import get_settings
        from nexus.integrations.search.engines import build_engine

        return build_engine(key, get_settings(), browser=browser)
    # Unknown token: fail safe to the offline stub rather than crashing startup.
    logger.warning("unknown search provider %r; using stub", name)
    return StubSearchProvider()


def get_search_provider() -> SearchProvider:
    global _search
    if _search is None:
        from nexus.core.config import get_settings

        _search = build_search_provider(get_settings().search_provider)
    return _search


def set_search_provider(provider: SearchProvider | None) -> None:
    global _search, _search_explicit, _exa
    _search = provider
    _search_explicit = provider is not None
    # Clearing the seam also drops the memoized strict provider, so a test that changed the Exa
    # keys in settings gets a provider built from the keys it set.
    _exa = None

