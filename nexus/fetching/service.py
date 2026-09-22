"""nexus-fetch — a stateless fetcher for signal collection, run on its own VM.

Two operations: fetch one URL, and scrape one search-results page. It holds no data, so the VM can
be destroyed and rebuilt at any time, and it stores nothing about a tenant — the caller decides what
to keep (`nexus/fetching/cache.py`). Deployment artifacts live in `services/fetch/`; the code lives
here so the offline suite covers it.

**Never publicly reachable.** A shared secret on every call (an EMPTY secret refuses everything
rather than meaning "open"), a firewall to the app's egress addresses, and `nexus.fetching.guard`
refusing private, loopback, link-local and metadata targets. An open fetch endpoint is an open proxy.

**Paced by design.** One request per host at a time and a small global cap, because the failure mode
of scraping is a burst, not a slow decay: an engine that tolerates two requests a minute for months
serves a captcha wall after thirty in one.

Scrapling is imported inside the default fetchers, so this module imports — and its behaviour can be
tested with injected fetchers — without the browser stack installed.
"""
from __future__ import annotations

import asyncio
import hmac
import importlib.util
import logging
import os
from urllib.parse import quote_plus, urlsplit

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from nexus.fetching.guard import UrlRejected, check_url
from nexus.fetching.parse import BlockedByEngine, parse_ddg

logger = logging.getLogger("nexus.fetching.service")

DDG_HTML = "https://html.duckduckgo.com/html/"
#: The most page content returned per fetch. Targets come from `account.domain`, which a tenant's
#: own reps control, so an oversized or slow-drip body must not be able to fill the response — or,
#: through JSON encoding, twice the memory — on a VM every tenant shares.
DEFAULT_MAX_CHARS = 2_000_000


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)
    recency_days: int = Field(default=0, ge=0, le=3650)


class FetchIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    mode: str = Field(default="http", pattern="^(http|stealth)$")
    timeout_s: float = Field(default=20.0, ge=1, le=60)


async def scrape_ddg(query: str, *, limit: int, recency_days: int) -> list[dict]:
    """Scrape DuckDuckGo's HTML endpoint with Scrapling's impersonating HTTP fetcher.

    `recency_days` is accepted for interface parity and not applied: the HTML endpoint has no
    reliable date filter, and the dorks already carry a lexical year constraint for `plain` engines.
    """
    from scrapling.fetchers import AsyncFetcher  # not installed in the offline suite

    page = await AsyncFetcher.get(
        f"{DDG_HTML}?q={quote_plus(query)}", stealthy_headers=True, timeout=20,
        follow_redirects="safe",
    )
    return parse_ddg(_html_of(page), limit=limit)


async def fetch_page(url: str, *, mode: str, timeout_s: float) -> dict:
    """Fetch one page. `http` is curl_cffi impersonation; `stealth` drives a real browser."""
    if mode == "stealth":
        from scrapling.fetchers import StealthyFetcher

        # Browser timeouts are milliseconds.
        page = await StealthyFetcher.async_fetch(url, headless=True, network_idle=True,
                                                 timeout=int(timeout_s * 1000))
    else:
        from scrapling.fetchers import AsyncFetcher

        # "safe" follows redirects but refuses ones that land on a private address — the guard
        # above only saw the FIRST url, and a public page redirecting to 169.254.169.254 is the
        # standard way past it.
        page = await AsyncFetcher.get(url, stealthy_headers=True, timeout=timeout_s,
                                      follow_redirects="safe")
    title = ""
    if hasattr(page, "css"):
        try:
            title = page.css("title::text").get() or ""
        except Exception:
            title = ""
    return {
        "status": int(getattr(page, "status", 200) or 200),
        "final_url": str(getattr(page, "url", url) or url),
        "html": _html_of(page),
        "text": page.get_all_text() if hasattr(page, "get_all_text") else "",
        "title": title,
    }


def _html_of(page) -> str:
    body = getattr(page, "html_content", None) or getattr(page, "body", None) or ""
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)


def _bounded(page: dict, max_chars: int) -> dict:
    """Cap the text a fetch returns, and say so rather than silently shortening it."""
    out = dict(page)
    truncated = False
    for key in ("html", "text"):
        value = out.get(key)
        if isinstance(value, str) and len(value) > max_chars:
            out[key] = value[:max_chars]
            truncated = True
    out["truncated"] = truncated
    return out


def build_app(*, token: str | None = None, search_fn=None, fetch_fn=None,
              concurrency: int | None = None, max_chars: int | None = None) -> FastAPI:
    """The ASGI app. The fetchers are injectable so tests never touch the network."""
    secret = os.environ.get("FETCH_TOKEN", "") if token is None else token
    cap = max_chars or int(os.environ.get("FETCH_MAX_CHARS", str(DEFAULT_MAX_CHARS)))
    search_fn = search_fn or scrape_ddg
    fetch_fn = fetch_fn or fetch_page
    # Per app, not per module: asyncio primitives bind to the first event loop that uses them.
    limit = asyncio.Semaphore(concurrency or int(os.environ.get("FETCH_CONCURRENCY", "4")))
    host_locks: dict[str, asyncio.Lock] = {}

    app = FastAPI(title="nexus-fetch", docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(supplied: str | None) -> None:
        # compare_digest: a token check that returns early on the first wrong byte leaks the token
        # one byte at a time to anyone who can time it.
        if not secret or not hmac.compare_digest(supplied or "", secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing token")

    def host_lock(host: str) -> asyncio.Lock:
        return host_locks.setdefault(host, asyncio.Lock())

    @app.get("/health")
    async def health(x_fetch_token: str | None = Header(default=None)) -> dict:
        # Token-gated like everything else: even "which tiers are installed" tells a stranger
        # what this box is. The app's health check sends the token.
        authorize(x_fetch_token)
        return {"ok": True, "browser": importlib.util.find_spec("scrapling") is not None}

    @app.post("/search")
    async def search(body: SearchIn, x_fetch_token: str | None = Header(default=None)) -> dict:
        authorize(x_fetch_token)
        async with limit, host_lock("duckduckgo.com"):
            try:
                hits = await search_fn(body.query, limit=body.limit, recency_days=body.recency_days)
            except BlockedByEngine:
                # 503, never an empty list: a refusal must not read as a quiet market.
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "blocked") from None
            except Exception as exc:
                logger.warning("search failed: %r", exc)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "search failed") from None
        engine = (hits[0].get("source") if hits else "") or "nexusfetch:ddg"
        return {"engine": engine, "hits": hits}

    @app.post("/fetch")
    async def fetch(body: FetchIn, x_fetch_token: str | None = Header(default=None)) -> dict:
        authorize(x_fetch_token)
        try:
            url = check_url(body.url)
        except UrlRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        async with limit, host_lock((urlsplit(url).hostname or "").lower()):
            try:
                page = await fetch_fn(url, mode=body.mode, timeout_s=body.timeout_s)
            except Exception as exc:
                # Logged here, never echoed: a fetcher's exception text can carry internal detail.
                logger.warning("fetch failed for %s: %r", url, exc)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "fetch failed") from None
        # The guard saw only the FIRST url. The HTTP tier refuses private redirects itself, but a
        # browser follows them, so where the fetch ENDED is checked too: a public page that
        # redirected to a metadata endpoint must not have that endpoint's content handed back.
        try:
            check_url(str(page.get("final_url") or url))
        except UrlRejected as exc:
            logger.warning("fetch for %s ended somewhere not fetchable: %s", url, exc)
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "redirected somewhere not fetchable") from None
        return _bounded(page, cap)

    return app
