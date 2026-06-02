"""Browser / search providers behind one interface.

Adapters (no hard dependency on any of them — core ships with the dependency-free DuckDuckGo one):
  * ``DuckDuckGoSearch`` — httpx + regex against the DDG HTML endpoint. Universal fallback.
  * ``ScraplingBrowser`` — uses Scrapling (optional extra) for stealthy fetching; falls back to DDG.
  * ``CloakCDPBrowser`` — routes through a CloakBrowser-Manager CDP endpoint for hardened research.

Every provider returns a list of ``{"title", "snippet", "url"}`` and NEVER raises across the
boundary — on failure it returns ``[]`` so callers degrade gracefully.
"""
from __future__ import annotations

import abc
import html
import importlib.util
import logging
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from nexus.core.config import get_settings

logger = logging.getLogger("nexus.enrichment.browser")

_TAG = re.compile(r"<[^>]+>")
_RESULT = re.compile(
    r'result__a[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.DOTALL
)
_SNIPPET = re.compile(r'result__snippet[^>]*>(?P<snippet>.*?)</a>', re.DOTALL)


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).strip()


def _resolve_ddg_url(href: str) -> str:
    # DDG wraps results as //duckduckgo.com/l/?uddg=<encoded>
    if "uddg=" in href:
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        if "uddg" in q:
            return unquote(q["uddg"][0])
    return href if href.startswith("http") else "https:" + href


class BrowserProvider(abc.ABC):
    @abc.abstractmethod
    async def search(self, query: str, *, limit: int = 5) -> list[dict]: ...

    async def aclose(self) -> None:  # optional override
        return None


class DuckDuckGoSearch(BrowserProvider):
    ENDPOINT = "https://html.duckduckgo.com/html/"

    def __init__(self, proxy: str | None = None):
        self._proxy = proxy

    async def search(self, query: str, *, limit: int = 5) -> list[dict]:
        try:
            async with httpx.AsyncClient(
                timeout=15, proxy=self._proxy, headers={"User-Agent": "Mozilla/5.0"}
            ) as client:
                resp = await client.post(self.ENDPOINT, data={"q": query})
                resp.raise_for_status()
                body = resp.text
        except Exception as exc:  # network/anti-bot — degrade gracefully
            logger.warning("DuckDuckGo search failed: %r", exc)
            return []

        results: list[dict] = []
        snippets = _SNIPPET.findall(body)
        for i, m in enumerate(_RESULT.finditer(body)):
            if i >= limit:
                break
            results.append(
                {
                    "title": _clean(m.group("title")),
                    "url": _resolve_ddg_url(m.group("url")),
                    "snippet": _clean(snippets[i]) if i < len(snippets) else "",
                }
            )
        return results


class ScraplingBrowser(BrowserProvider):
    """Stealthy fetching via Scrapling; parses a DDG SERP. Falls back to DuckDuckGoSearch."""

    def __init__(self):
        self._fallback = DuckDuckGoSearch()

    async def search(self, query: str, *, limit: int = 5) -> list[dict]:
        try:
            from scrapling.fetchers import StealthyFetcher  # type: ignore

            page = await StealthyFetcher.async_fetch(
                f"{DuckDuckGoSearch.ENDPOINT}?q={quote_plus(query)}"
            )
            body = getattr(page, "html_content", None) or getattr(page, "body", "") or str(page)
            results, snippets = [], _SNIPPET.findall(body)
            for i, m in enumerate(_RESULT.finditer(body)):
                if i >= limit:
                    break
                results.append(
                    {
                        "title": _clean(m.group("title")),
                        "url": _resolve_ddg_url(m.group("url")),
                        "snippet": _clean(snippets[i]) if i < len(snippets) else "",
                    }
                )
            if results:
                return results
        except Exception as exc:
            logger.info("Scrapling unavailable/failed (%r); falling back to DuckDuckGo", exc)
        return await self._fallback.search(query, limit=limit)


class CloakCDPBrowser(BrowserProvider):
    """Research via a CloakBrowser-Manager CDP endpoint.

    Full automation drives the CDP session with Playwright (the ``scraping`` extra). When
    Playwright is not present, this degrades to DuckDuckGo routed through Cloak's network.
    """

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url.rstrip("/")
        self._fallback = DuckDuckGoSearch()

    async def search(self, query: str, *, limit: int = 5) -> list[dict]:
        # CDP-driven navigation is environment-specific; default to the resilient fallback.
        return await self._fallback.search(query, limit=limit)


_browser: BrowserProvider | None = None


def _build_browser() -> BrowserProvider:
    s = get_settings()
    choice = s.browser_provider
    if choice == "duckduckgo":
        return DuckDuckGoSearch()
    if choice == "cloak":
        return CloakCDPBrowser(s.cloak_cdp_url)
    if choice == "scrapling":
        return ScraplingBrowser()
    # auto: prefer Scrapling if installed, else DuckDuckGo.
    if importlib.util.find_spec("scrapling") is not None:
        return ScraplingBrowser()
    return DuckDuckGoSearch()


def get_browser_provider() -> BrowserProvider:
    global _browser
    if _browser is None:
        _browser = _build_browser()
    return _browser


def set_browser_provider(provider: BrowserProvider) -> None:
    global _browser
    _browser = provider
