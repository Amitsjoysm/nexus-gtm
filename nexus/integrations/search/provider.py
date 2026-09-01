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
    global _search
    _search = provider


def provider_for_task(task: str):
    """The search provider for ONE task, honouring its per-task override.

    `search_provider` is global, but the tasks behind it have wildly different value per query.
    Measured on the live deployment: account enrichment alone was 123 of the billed search events
    across 56 accounts, all on Exa — while `find_similar` (lookalikes) and `search_companies`
    (ICP/company discovery) are the ONLY capabilities that genuinely need Exa, because every other
    provider returns `[]` for them. Everything else is a plain query any index answers, so pointing
    the bulk work somewhere cheaper costs nothing in capability.

    Mirrors how `signal_search_provider` already works, including the important part: this uses
    ``build_search_provider`` rather than the global singleton, so selecting a provider for one task
    must not replace the one the rest of the application resolved.

    An empty or unknown setting falls back to the global provider, so a deployment that configures
    none of these behaves exactly as it did before they existed.
    """
    from nexus.core.config import get_settings

    choice = (getattr(get_settings(), f"{task}_search_provider", "") or "").strip()
    return build_search_provider(choice) if choice else get_search_provider()


class ChainedSearchProvider(SearchProvider):
    """Try each provider in turn; the first with results wins.

    Built for contact discovery, where recall matters more than cost: a missed contact is a rep with
    nobody to call, and the two indexes genuinely disagree — Exa's semantic matching is better at
    people queries, while an operator SERP catches pages Exa's index has not embedded.

    Sequential and short-circuiting, NOT a fan-out. Querying every provider on every call would
    double the bill for the (common) case where the first one already answered, and cost is the
    reason this split exists at all. The second provider is only paid for when the first found
    nothing.

    ``query_dialect`` comes from the FIRST provider: the caller builds its query string before
    calling, so it can only be shaped for one dialect, and the first is the one that usually serves.
    Capability methods (`find_similar`, `search_companies`) deliberately are NOT chained — they are
    Exa-only, and a chain that silently returned `[]` from a second provider would look like "no
    lookalikes exist" rather than "this provider cannot do that".
    """

    name = "chain"

    def __init__(self, providers: list[SearchProvider]):
        self.providers = [p for p in providers if p is not None]
        self.query_dialect = getattr(
            self.providers[0], "query_dialect", "plain"
        ) if self.providers else "plain"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        for provider in self.providers:
            try:
                hits = await provider.search(query, limit=limit)
            except Exception:  # one dead provider must not sink the others
                continue
            if hits:
                return hits
        return []

def provider_for_task_chain(task: str, *, browser=None) -> SearchProvider:
    """Like :func:`provider_for_task`, but a comma-separated setting builds a fallback CHAIN.

    ``contact_search_provider = "exa,firecrawl"`` means: ask Exa, and only pay Firecrawl when Exa
    found nothing. One value behaves exactly like `provider_for_task`; empty falls back to the
    global provider. So a deployment that configures nothing is unaffected.
    """
    from nexus.core.config import get_settings

    raw = (getattr(get_settings(), f"{task}_search_provider", "") or "").strip()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return get_search_provider()
    if len(names) == 1:
        return build_search_provider(names[0], browser=browser)
    return ChainedSearchProvider(
        [build_search_provider(n, browser=browser) for n in names]
    )
