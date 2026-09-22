"""Try the cheap backend first, fall through to the paid one, then to the keyless floor.

Collection degrades rather than stops — the posture every provider seam here takes.

Two rules that are easy to get wrong:

* **The chain declares the dialect of its WEAKEST member.** The dork renders its query BEFORE the
  search and cannot know which member will answer. An `operator` query returns zero results on a
  `plain` engine, silently; a `plain` query merely loses some precision on an operator engine. So
  with the self-hosted fetcher on, a Firecrawl fallback receives the plain phrasing — a small,
  deliberate precision cost on the path that should now be rare.
* **An empty answer with no failure is an ANSWER.** Falling through on it would pay the next
  provider for a question already answered, and "nothing was published" is the common case.
"""
from __future__ import annotations

import logging

from nexus.integrations.search.provider import SearchHit, SearchProvider

logger = logging.getLogger("nexus.search.fallback")

_DIALECT_RANK = {"plain": 0, "operator": 1, "semantic": 2}


class FallbackSearchProvider(SearchProvider):
    name = "fallback"

    def __init__(self, providers: list) -> None:
        self.providers = [p for p in providers if p is not None]
        #: Which member answered the last query, empty when none did. Recorded in provenance.
        self.answered_by = ""

    @property
    def query_dialect(self) -> str:  # type: ignore[override]
        dialects = [getattr(p, "query_dialect", "plain") for p in self.providers]
        return min(dialects, key=lambda d: _DIALECT_RANK.get(d, 0)) if dialects else "plain"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return await self._run(query, recent=False, limit=limit)

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90,
                            include_domains: tuple[str, ...] = (),
                            exclude_domains: tuple[str, ...] = ()) -> list[SearchHit]:
        return await self._run(query, recent=True, limit=limit, days=days,
                               include_domains=include_domains, exclude_domains=exclude_domains)

    async def _ask(self, provider, query: str, *, recent: bool, limit: int, **kwargs):
        if not recent:
            return await provider.search(query, limit=limit)
        try:
            return await provider.search_recent(query, limit=limit, **kwargs)
        except TypeError:
            # A member without the structured-domain kwargs; the keyword dialect already carries
            # the domains inline, the same fallback `DorkedSearchSource._run` uses.
            return await provider.search_recent(query, limit=limit, days=kwargs.get("days", 90))

    async def _run(self, query: str, *, recent: bool, limit: int, **kwargs) -> list[SearchHit]:
        failures: list[str] = []
        for provider in self.providers:
            name = getattr(provider, "name", "unknown")
            try:
                hits = await self._ask(provider, query, recent=recent, limit=limit, **kwargs)
            except Exception as exc:
                logger.warning("search via %s failed: %r", name, exc)
                failures.append(f"{name}: {type(exc).__name__}")
                continue
            failure = str(getattr(provider, "last_failure", "") or "")
            if failure:
                failures.append(f"{name}: {failure}")
                continue
            self.answered_by = name
            self.last_failure = ""
            return list(hits or [])
        self.answered_by = ""
        self.last_failure = "; ".join(failures)
        return []
