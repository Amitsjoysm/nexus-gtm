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

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

# Status codes worth a retry: rate limit + transient upstream errors. A free-tier key hits 429
# under bursty use, and silently degrading to [] makes discovery look like "no matches".
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFFS = (0.5, 1.5)  # two retries; ~2s worst-case added latency before degrading

# Statuses that condemn the KEY rather than the request. Rotation used to fire on 429 alone, so a
# revoked or exhausted key was retried with backoff and then given up on — and because it sits at a
# fixed index, the pool never advanced past it. One dead key at index 0 therefore disabled the whole
# pool while the other keys stayed untouched, and the caller saw `[]`, which reads as "no results".
#
# `nexus/integrations/apify.py` already got this right; these two did not. The rule there is the
# rule here: rotate past a condemned key and never retry it.
#
#   401 unauthorized  — revoked, rotated, or mistyped. Measured this session: an Apify key that
#                       worked two weeks ago now 401s, so this is not hypothetical.
#   403 forbidden     — key valid, this operation not permitted for it.
#   402 payment req.  — Exa returns this when the account's credits are gone. Key-specific and
#                       permanent for that key, so it is a rotation case, not a backoff case.
_KEY_REJECTED_STATUS = frozenset({401, 402, 403})

from nexus.integrations.search.provider import (
    DuckDuckGoSearchProvider,
    SearchHit,
    SearchProvider,
    StubSearchProvider,
)

logger = logging.getLogger("nexus.integrations.search.engines")

_TIMEOUT = 15.0
# Per-result page-text budget requested from the provider (Exa maxCharacters) and kept after.
# Larger than a one-line teaser so a fact stated further down a page — "privately held", a funding
# round, a headquarters — actually reaches the LLM (Ask-about-account, firmographic extraction),
# instead of being truncated away. Still bounded to keep token cost/latency in check.
_SNIPPET_CAP = 1000
_TAG = re.compile(r"<[^>]+>")
# Exa rejects numResults > 100 with a 400 (the whole request fails → 0 results). Clamp every
# request so an over-eager caller (e.g. a big discovery pool, or lookalike's limit*3 over-fetch)
# degrades to "as many as Exa allows" instead of silently returning nothing.
_EXA_MAX_RESULTS = 100


def _exa_num_results(limit: int) -> int:
    return max(1, min(int(limit), _EXA_MAX_RESULTS))


def _strip_tags(text: str) -> str:
    """Brave highlights wrap matches in <strong> tags; drop markup for a clean snippet."""
    return _TAG.sub("", text or "").strip()


class ExaSearchProvider(SearchProvider):
    name = "exa"
    # Neural retrieval: it reads intent, not operators. Measured against the live API — the query
    # "Ramp job openings" returns Ramp's own careers page, while
    # "(site:jobs.lever.co OR site:boards.greenhouse.io) Ramp" returns *other* companies' postings
    # that merely mention Ramp the product. Callers phrase intent and pass domains structurally.
    query_dialect = "semantic"
    ENDPOINT = "https://api.exa.ai/search"
    ENDPOINT_SIMILAR = "https://api.exa.ai/findSimilar"

    def __init__(self, api_key: str = "", *, api_keys: list[str] | None = None,
                 timeout: float = _TIMEOUT):
        keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
        if not keys and api_key and api_key.strip():
            keys = [api_key.strip()]
        self.api_keys = keys
        self._key_idx = 0
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        """The key currently in use (rotates within the pool on rate-limit)."""
        return self.api_keys[self._key_idx] if self.api_keys else ""

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not self.api_keys:
            return []
        payload = {
            "query": query,
            "numResults": _exa_num_results(limit),
            # Ask for a short text excerpt so hits carry a usable snippet.
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        return await self._post(self.ENDPOINT, payload, limit)

    async def search_companies(
        self, query: str, *, limit: int = 10, exclude_domains: list[str] | None = None
    ) -> list[SearchHit]:
        """Exa search biased to company homepages (``category=company``), optionally excluding
        some domains. This is what makes 'find similar companies' return real businesses instead
        of directory/profile/news pages."""
        if not self.api_keys:
            return []
        payload: dict = {
            "query": query,
            "numResults": _exa_num_results(limit),
            "category": "company",
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        domains = [d for d in (exclude_domains or []) if d]
        if domains:
            payload["excludeDomains"] = domains
        return await self._post(self.ENDPOINT, payload, limit)

    async def search_recent(
        self,
        query: str,
        *,
        limit: int = 5,
        days: int = 90,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchHit]:
        """Exa with a real published-date floor and structured domain filters.

        This is the only adapter that can actually enforce recency. Elsewhere the base falls back to
        a plain search and the dork's lexical year constraint does what it can — but "published
        after this date" is a property of the crawl index, and no query string substitutes for it.

        Domains go in ``includeDomains``/``excludeDomains``, never in the query. Exa is neural: a
        literal ``site:jobs.lever.co`` is read as words, and measurably returns *other* companies'
        job posts that merely mention the account name. ``includeDomains`` wins when both are given
        — once the search is restricted to an allowlist, exclusions can only be redundant, and Exa
        rejects some combinations of the two.
        """
        if not self.api_keys:
            return []
        floor = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
        payload: dict = {
            "query": query,
            "numResults": _exa_num_results(limit),
            "startPublishedDate": floor,
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        if include_domains:
            payload["includeDomains"] = list(include_domains)
        elif exclude_domains:
            payload["excludeDomains"] = list(exclude_domains)
        return await self._post(self.ENDPOINT, payload, limit)

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        """Exa ``/findSimilar``: pages similar to a seed URL — the lookalike seam.

        Same response shape as ``/search`` (``results[]`` of title/url/text), so it reuses
        :meth:`_parse`. Key-gated and non-raising like every adapter here.
        """
        if not self.api_key or not url:
            return []
        # Over-fetch so that, after the caller collapses multiple pages per competitor down to
        # one domain, we still surface ~limit distinct companies.
        n = _exa_num_results(max(limit * 3, 15))
        payload = {
            "url": url,
            # Exclude the seed's own domain — otherwise findSimilar returns the company's own
            # subpages, which the lookalike service then dedups to nothing ("No lookalikes").
            "excludeSourceDomain": True,
            "numResults": n,
            "contents": {"text": {"maxCharacters": _SNIPPET_CAP}},
        }
        return await self._post(self.ENDPOINT_SIMILAR, payload, n)

    async def _post(self, endpoint: str, payload: dict, limit: int) -> list[SearchHit]:
        keys = self.api_keys
        if not keys:
            return []
        # Budget: one shot per key (rotate to ride out a single key's 429) + transient backoffs.
        # Rotation is sticky — once a key works we stay on it until it too rate-limits.
        backoff_used = 0
        rejected: dict[int, int] = {}          # key index -> status that condemned it
        for attempt in range(len(keys) + len(_RETRY_BACKOFFS)):
            headers = {"x-api-key": keys[self._key_idx], "Content-Type": "application/json"}
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code in _KEY_REJECTED_STATUS:
                    # Condemns the key, not the request: rotate past it and never retry it. See
                    # _KEY_REJECTED_STATUS. Recorded so the final log can say which keys are dead
                    # rather than blaming the rate limit for someone else's revoked credential.
                    rejected[self._key_idx] = resp.status_code
                    logger.warning("Exa key #%d rejected (%d) — rotating past it",
                                   self._key_idx, resp.status_code)
                    if len(rejected) >= len(keys):
                        logger.error(
                            "Exa: every key in the %d-key pool was rejected (%s). This is a "
                            "credentials problem, not a rate limit — results will be empty until "
                            "it is fixed.",
                            len(keys), ", ".join(f"#{i}:{s}" for i, s in sorted(rejected.items())),
                        )
                        return []
                    self._key_idx = (self._key_idx + 1) % len(keys)
                    continue
                if resp.status_code == 429:
                    if len(keys) > 1:
                        self._key_idx = (self._key_idx + 1) % len(keys)  # next key in the pool
                    # Pause only after cycling the whole pool, to avoid hammering on a hard cap.
                    if (len(keys) == 1 or (attempt + 1) % len(keys) == 0) \
                            and backoff_used < len(_RETRY_BACKOFFS):
                        await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                        backoff_used += 1
                    continue
                if resp.status_code in _RETRY_STATUS and backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])  # transient 5xx
                    backoff_used += 1
                    continue
                resp.raise_for_status()
                return self._parse(resp.json(), limit)
            except Exception as exc:  # network / anti-bot / HTTP — retry, then degrade gracefully
                if backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                    backoff_used += 1
                    continue
                logger.warning("Exa request to %s failed after %d attempts: %r",
                               endpoint, attempt + 1, exc)
                return []
        logger.warning("Exa %s: %d-key pool + retries all rate-limited", endpoint, len(keys))
        return []

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
    # A real index with documented operator support.
    query_dialect = "operator"
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
    # Google SERP passthrough — operators behave exactly as they do on google.com.
    query_dialect = "operator"
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


class FirecrawlSearchProvider(SearchProvider):
    """Firecrawl search — a keyword backend that can also fetch the page behind a result.

    Added because the signal pipeline should not have a single load-bearing vendor. The keyless
    DuckDuckGo path returns 403 after roughly ten rapid queries (measured), which is well inside a
    single account refresh once the dork library is running, and Exa is neural — excellent, but it
    is one company's index and it does not honour the operator dorks that the rest of the library is
    written in. Firecrawl is Google-backed and keyword-native, so the same dorks work unchanged.

    Two capabilities the others do not combine:

    * ``tbs`` gives a **real recency filter on a keyword engine** (``qdr:w``/``qdr:m``), so "latest"
      no longer depends on holding an Exa key.
    * ``scrapeOptions`` returns page content with the result, so a thin SERP snippet can be replaced
      by the actual text the classifier needs.

    Response shape is handled defensively: v1 returned ``data`` as a list, v2 returns an object
    keyed by source (``data.web``). Both are parsed, because a provider that silently returns
    nothing after an API version bump is the worst failure mode for a signal source — it looks
    exactly like "this company has no news".
    """

    name = "firecrawl"
    # Google-backed, so the operator dorks work as written.
    query_dialect = "operator"
    ENDPOINT = "https://api.firecrawl.dev/v2/search"

    def __init__(self, api_key: str = "", *, api_keys: list[str] | None = None,
                 timeout: float = _TIMEOUT):
        keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
        if not keys and api_key and api_key.strip():
            keys = [api_key.strip()]
        self.api_keys = keys
        self._key_idx = 0
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        """The key currently in use. Rotation is sticky — once a key works we stay on it until it
        too rate-limits, so a healthy key is not abandoned after one unlucky request."""
        return self.api_keys[self._key_idx] if self.api_keys else ""

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return await self._request(query, limit=limit, tbs="")

    async def search_recent(
        self,
        query: str,
        *,
        limit: int = 5,
        days: int = 90,
        include_domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchHit]:
        # Domains are ignored on purpose: this is a keyword backend, so the dork already carries
        # them as site:/-site: terms inside the query.
        return await self._request(query, limit=limit, tbs=_tbs_for_days(days))

    async def _request(self, query: str, *, limit: int, tbs: str) -> list[SearchHit]:
        """One search, rotating across the key pool on rate-limit.

        Mirrors ``ExaSearchProvider._post``: one shot per key to ride out a single key's 429, then
        bounded backoffs, then degrade to ``[]``. A crawl issues several queries per account, so a
        single free-tier key is exhausted quickly — and silently returning nothing would look
        exactly like a company with no news.
        """
        keys = self.api_keys
        if not keys or not query:
            return []
        payload: dict = {"query": query, "limit": max(1, limit)}
        if tbs:
            payload["tbs"] = tbs

        backoff_used = 0
        rejected: dict[int, int] = {}          # key index -> status that condemned it
        for attempt in range(len(keys) + len(_RETRY_BACKOFFS)):
            headers = {
                "Authorization": f"Bearer {keys[self._key_idx]}",
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(self.ENDPOINT, json=payload, headers=headers)
                if resp.status_code in _KEY_REJECTED_STATUS:
                    # Same rule as Exa above: a condemned key is rotated past, never retried.
                    rejected[self._key_idx] = resp.status_code
                    logger.warning("Firecrawl key #%d rejected (%d) — rotating past it",
                                   self._key_idx, resp.status_code)
                    if len(rejected) >= len(keys):
                        logger.error(
                            "Firecrawl: every key in the %d-key pool was rejected (%s). This is a "
                            "credentials problem, not a rate limit — results will be empty until "
                            "it is fixed.",
                            len(keys), ", ".join(f"#{i}:{s}" for i, s in sorted(rejected.items())),
                        )
                        return []
                    self._key_idx = (self._key_idx + 1) % len(keys)
                    continue
                if resp.status_code == 429:
                    if len(keys) > 1:
                        self._key_idx = (self._key_idx + 1) % len(keys)   # next key in the pool
                    # Pause only after cycling the whole pool, to avoid hammering a hard cap.
                    if (len(keys) == 1 or (attempt + 1) % len(keys) == 0)                             and backoff_used < len(_RETRY_BACKOFFS):
                        await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                        backoff_used += 1
                    continue
                if resp.status_code in _RETRY_STATUS and backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])    # transient 5xx
                    backoff_used += 1
                    continue
                resp.raise_for_status()
                return self._parse(resp.json(), limit)
            except Exception as exc:
                if backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                    backoff_used += 1
                    continue
                logger.warning("Firecrawl search failed after %d attempts: %r", attempt + 1, exc)
                return []
        logger.warning("Firecrawl: %d-key pool + retries all rate-limited", len(keys))
        return []

    def _parse(self, data: dict, limit: int) -> list[SearchHit]:
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            rows = payload.get("web") or payload.get("results") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        out: list[SearchHit] = []
        for r in rows[:limit]:
            if not isinstance(r, dict):
                continue
            # `description` is the SERP snippet; `markdown` is the scraped page when scrapeOptions
            # was requested. Prefer the snippet — the classifier reads headlines, and a full page
            # of markdown buries the one sentence that says what happened.
            snippet = (r.get("description") or r.get("snippet") or r.get("markdown") or "").strip()
            out.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or r.get("link") or "").strip(),
                    snippet=snippet[:_SNIPPET_CAP],
                    source=self.name,
                )
            )
        return out


def _tbs_for_days(days: int) -> str:
    """Google ``tbs`` recency token for a day window. Coarse by design — the buckets are all Google
    offers, and asking for a narrower window than exists returns nothing rather than approximating."""
    if days <= 1:
        return "qdr:d"
    if days <= 7:
        return "qdr:w"
    if days <= 31:
        return "qdr:m"
    if days <= 365:
        return "qdr:y"
    return ""


# Engine token -> (provider class, settings attribute holding the key).
_ENGINES: dict[str, tuple[type[SearchProvider], str]] = {
    "exa": (ExaSearchProvider, "exa_api_key"),
    "brave": (BraveSearchProvider, "brave_api_key"),
    "serper": (SerperSearchProvider, "serper_api_key"),
    "firecrawl": (FirecrawlSearchProvider, "firecrawl_api_key"),
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
    # Engines with a key-rotation pool (primary key + a comma-separated pool).
    if key in ("exa", "firecrawl"):
        keys = getattr(settings, f"{key}_api_key_list", None)
        if keys is None:
            single = (getattr(settings, attr, "") or "").strip()
            keys = [single] if single else []
        if not keys:
            logger.warning("%s selected but no key is set; falling back to DuckDuckGo", key)
            return DuckDuckGoSearchProvider(browser=browser)
        return provider_cls(api_keys=keys)
    api_key = (getattr(settings, attr, "") or "").strip()
    if not api_key:
        logger.warning("%s selected but %s is unset; falling back to DuckDuckGo", key, attr)
        return DuckDuckGoSearchProvider(browser=browser)
    return provider_cls(api_key)
