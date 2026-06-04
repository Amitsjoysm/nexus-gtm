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

    @abc.abstractmethod
    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]: ...

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        """Find pages similar to ``url`` (powers the find-lookalike play).

        Optional capability: the default returns ``[]`` so providers that can't do
        similarity search (DuckDuckGo, stub) degrade quietly and offline/CI stays
        deterministic. Adapters that can (Exa) override this.
        """
        return []


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
    if key in ("exa", "brave", "serper"):
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
