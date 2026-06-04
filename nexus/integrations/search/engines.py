# nexus/integrations/search/engines.py
"""Hosted web-search backends behind the :class:`SearchProvider` seam.

Three production search APIs, one normalized interface (:class:`SearchHit`):

  * :class:`ExaSearchProvider`    — Exa neural/keyword search. POST ``https://api.exa.ai/search``,
    auth header ``x-api-key``; results under ``results[]`` (``title``/``url``/``text``).
  * :class:`BraveSearchProvider`  — Brave Web Search. GET ``…/res/v1/web/search``, auth header
    ``X-Subscription-Token``; results under ``web.results[]`` (``title``/``url``/``description``).
  * :class:`SerperSearchProvider` — Serper (Google SERP). POST ``https://google.serper.dev/search``,
    auth header ``X-API-KEY``; results under ``organic[]`` (``title``/``link``/``snippet``).

Contract notes are kept here so a reviewer (Codex) can confirm them against current vendor docs.

Two invariants every adapter upholds, so the registry waterfall and offline CI stay safe:
  1. **Key-gated** — with no API key the adapter short-circuits to ``[]`` (never touches the
     network). :func:`build_engine` degrades a keyless selection to keyless DuckDuckGo instead.
  2. **Never raises** — any network/HTTP/parse failure is logged and returns ``[]``.

Response parsing is split into pure ``_parse`` helpers so it is unit-tested without the network.
"""
from __future__ import annotations

import logging
import re

import httpx

from nexus.integrations.search.provider import (
    DuckDuckGoSearchProvider,
    SearchHit,
    SearchProvider,
    StubSearchProvider,
)

logger = logging.getLogger("nexus.integrations.search.engines")

_TIMEOUT = 15.0
_SNIPPET_CAP = 320
_TAG = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    """Brave highlights wrap matches in <strong> tags; drop markup for a clean snippet."""
    return _TAG.sub("", text or "").strip()


class ExaSearchProvider(SearchProvider):
    name = "exa"
    ENDPOINT = "https://api.exa.ai/search"
    ENDPOINT_SIMILAR = "https://api.exa.ai/findSimilar"

    def __init__(self, api_key: str, *, timeout: float = _TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not self.api_key:
            return []
        payload = {
            "query": query,
            "numResults": limit,
            # Ask for a short text excerpt so hits carry a usable snippet.
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        return await self._post(self.ENDPOINT, payload, limit)

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        """Exa ``/findSimilar``: pages similar to a seed URL — the lookalike seam.

        Same response shape as ``/search`` (``results[]`` of title/url/text), so it reuses
        :meth:`_parse`. Key-gated and non-raising like every adapter here.
        """
        if not self.api_key or not url:
            return []
        payload = {
            "url": url,
            "numResults": limit,
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        return await self._post(self.ENDPOINT_SIMILAR, payload, limit)

    async def _post(self, endpoint: str, payload: dict, limit: int) -> list[SearchHit]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # network / anti-bot / HTTP — degrade gracefully
            logger.warning("Exa request to %s failed: %r", endpoint, exc)
            return []
        return self._parse(data, limit)

    def _parse(self, data: dict, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for r in (data.get("results") or [])[:limit]:
            snippet = (r.get("text") or "").strip()
            if not snippet and r.get("highlights"):
                snippet = " ".join(r.get("highlights") or []).strip()
            out.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or "").strip(),
                    snippet=snippet[:_SNIPPET_CAP],
                    source=self.name,
                )
            )
        return out


class BraveSearchProvider(SearchProvider):
    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, *, timeout: float = _TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not self.api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    self.ENDPOINT,
                    params={"q": query, "count": limit},
                    headers={
                        "X-Subscription-Token": self.api_key,
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Brave search failed: %r", exc)
            return []
        return self._parse(data, limit)

    def _parse(self, data: dict, limit: int) -> list[SearchHit]:
        results = ((data.get("web") or {}).get("results")) or []
        out: list[SearchHit] = []
        for r in results[:limit]:
            out.append(
                SearchHit(
                    title=_strip_tags(r.get("title") or ""),
                    url=(r.get("url") or "").strip(),
                    snippet=_strip_tags(r.get("description") or "")[:_SNIPPET_CAP],
                    source=self.name,
                )
            )
        return out


class SerperSearchProvider(SearchProvider):
    name = "serper"
    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, api_key: str, *, timeout: float = _TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not self.api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.ENDPOINT,
                    json={"q": query, "num": limit},
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Serper search failed: %r", exc)
            return []
        return self._parse(data, limit)

    def _parse(self, data: dict, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for r in (data.get("organic") or [])[:limit]:
            out.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("link") or "").strip(),
                    snippet=(r.get("snippet") or "").strip()[:_SNIPPET_CAP],
                    source=self.name,
                )
            )
        return out


# Engine token -> (provider class, settings attribute holding the key).
_ENGINES: dict[str, tuple[type[SearchProvider], str]] = {
    "exa": (ExaSearchProvider, "exa_api_key"),
    "brave": (BraveSearchProvider, "brave_api_key"),
    "serper": (SerperSearchProvider, "serper_api_key"),
}


def build_engine(name: str, settings, *, browser=None) -> SearchProvider:
    """Build a hosted-engine provider, reading its key from settings.

    With no key for the requested engine, fall back to keyless DuckDuckGo so web search keeps
    working rather than silently going dark. An unknown token falls back to the offline stub.
    """
    key = (name or "").strip().lower()
    spec = _ENGINES.get(key)
    if spec is None:
        logger.warning("unknown search engine %r; using stub", name)
        return StubSearchProvider()
    provider_cls, attr = spec
    api_key = (getattr(settings, attr, "") or "").strip()
    if not api_key:
        logger.warning("%s selected but %s is unset; falling back to DuckDuckGo", key, attr)
        return DuckDuckGoSearchProvider(browser=browser)
    return provider_cls(api_key)
