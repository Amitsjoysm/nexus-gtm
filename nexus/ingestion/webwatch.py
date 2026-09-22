# nexus/ingestion/webwatch.py
"""Website change monitoring: fetch, normalise, hash, diff.

Ours end to end — no third party, no key, no scraping of anyone's private surface. We fetch a
handful of a company's own public pages and notice when they change.

**Normalisation is the whole feature, not a detail.** Measured against live sites, hashing the raw
HTML is unstable on most of them: linear.app/pricing and ramp.com/security produce a different
digest on two fetches seconds apart, because modern pages carry build ids, CSRF tokens, nonces,
cache-busting asset URLs and inlined JSON state that changes per request. A naive watcher reports
"pricing changed" every single time it runs, which is indistinguishable from noise and trains
whoever sees it to ignore the signal.

Stripping scripts, styles and markup, then collapsing whitespace, was stable across repeated
fetches on every page tested. That is the difference between a working feature and a permanent
false alarm, so the normaliser has its own tests pinned to real-world nuisances.

The pages watched are chosen because a change to each *means* something commercially:

* ``pricing``  — the strongest. A pricing change is a strategy change, and it is the page a rep can
  open a conversation about.
* ``careers``  — corroborates the ATS signal from a second direction.
* ``security`` — a new SOC 2 / ISO badge is a compliance milestone and a buying trigger.
* ``about``    — leadership and positioning changes surface here first.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("nexus.ingestion.webwatch")

_UA = "NexusGTM/1.0 (+signals)"

# Page kinds and the paths they conventionally live at, best first.
WATCHED_PAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pricing", ("/pricing", "/plans", "/pricing/")),
    ("security", ("/security", "/trust", "/security/")),
    ("careers", ("/careers", "/jobs", "/careers/")),
    ("about", ("/about", "/company", "/about-us")),
)

# Whole elements whose content is never page *meaning*: behaviour, presentation, and inlined state.
_DROP_ELEMENTS = re.compile(
    r"(?is)<(script|style|noscript|svg|template|iframe)[^>]*>.*?</\1\s*>"
)
_TAGS = re.compile(r"(?s)<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# Volatile fragments that survive tag-stripping and would still churn the digest: build hashes,
# nonces, ISO timestamps and cache-busting query strings.
_VOLATILE = (
    re.compile(r"\b[0-9a-f]{16,}\b"),                        # build ids / content hashes
    re.compile(r"\b\d{4}-\d{2}-\d{2}t[\d:.]+z?\b"),          # ISO timestamps (already lowercased)
    re.compile(r"[?&](?:v|ts|cb|_)=[^\s&\"']+"),             # cache busters
)


def normalise(html: str) -> str:
    """Reduce a page to its readable text, stably.

    Order matters: drop whole script/style elements *before* stripping tags, or their bodies become
    part of the text and every build id lands in the digest.
    """
    text = _DROP_ELEMENTS.sub(" ", html or "")
    text = _TAGS.sub(" ", text)
    text = text.lower()
    for pattern in _VOLATILE:
        text = pattern.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def content_hash(html: str) -> str:
    return hashlib.sha256(normalise(html).encode("utf-8", "ignore")).hexdigest()


@dataclass(slots=True)
class PageCheck:
    """One page fetched and normalised."""

    page_kind: str
    url: str
    outcome: str = "ok"        # ok | not_found | error
    digest: str = ""
    text: str = ""
    error: str = ""


#: Pages larger than this are fetched but not cached. A careers page measured 3.7 MB of JavaScript;
#: storing pages that size for every watched account would trade a search bill for a storage one.
_MAX_CACHED_PAGE_CHARS = 1_000_000


async def _direct_get(url: str) -> tuple[int, str]:
    """(status, body) fetched from this process with plain httpx. Never raises."""
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            return resp.status_code, resp.text
    except Exception as exc:
        logger.warning("page fetch failed for %s: %r", url, exc)
        return 0, ""


async def _get(url: str, fetch=None) -> tuple[int, str]:
    """(status, body). Never raises.

    An injected `fetch` is used as given — the seam the tests drive. Otherwise the page comes from,
    in order: the shared `web_cache` (a page fetched for one tenant is reused for the next within
    `page_cache_ttl_s`), the nexus-fetch service when it is configured (these are the pages most
    likely to sit behind Cloudflare or be rendered by JavaScript, which plain httpx cannot read),
    and plain httpx from this process — so a deployment without the service watches pages exactly
    as it did before. Only a 200 with a body is cached: a failure replayed for a day would read as
    "the page vanished" when it was a blip.
    """
    if fetch is not None:
        try:
            return await fetch(url)
        except Exception as exc:
            logger.warning("page fetch failed for %s: %r", url, exc)
            return 0, ""

    from nexus.fetching import cache
    from nexus.fetching.client import get_fetch_client
    from nexus.fetching.ttl import ttl_for_page

    cached = await cache.get("page", url, engine="page")
    if isinstance(cached, dict) and cached.get("html"):
        return int(cached.get("status") or 200), str(cached["html"])

    status, body = 0, ""
    client = get_fetch_client()
    if client.configured:
        page = await client.fetch_page(url, mode="http")
        if page and page.get("html"):
            status, body = int(page.get("status") or 200), str(page["html"])
    if not body:
        status, body = await _direct_get(url)

    if status == 200 and body and len(body) <= _MAX_CACHED_PAGE_CHARS:
        await cache.put("page", url, engine="page", payload={"status": status, "html": body},
                        ttl_s=ttl_for_page())
    return status, body


async def check_page(domain: str, page_kind: str, paths: tuple[str, ...], *, fetch=None) -> PageCheck:
    """Fetch the first path that exists for this page kind."""
    for path in paths:
        url = f"https://{domain}{path}"
        status, body = await _get(url, fetch)
        if status == 200 and body:
            return PageCheck(page_kind=page_kind, url=url, outcome="ok",
                             digest=content_hash(body), text=normalise(body))
        if status == 0:
            return PageCheck(page_kind=page_kind, url=url, outcome="error",
                             error="fetch failed")
    return PageCheck(page_kind=page_kind, url=f"https://{domain}{paths[0]}",
                     outcome="not_found")


def summarise_change(previous: str, current: str, *, max_len: int = 300) -> str:
    """A short, human description of what changed.

    ``difflib`` from the standard library, deliberately: the point is to give a rep a sentence they
    can open with, not to render a diff. Added lines are what matters — "they added an Enterprise
    tier" is a conversation, "they removed a footer link" is not.
    """
    import difflib

    old_words = (previous or "").split()
    new_words = (current or "").split()
    added = [
        w for tag, _i1, _i2, j1, j2 in
        difflib.SequenceMatcher(None, old_words, new_words, autojunk=False).get_opcodes()
        if tag in ("insert", "replace")
        for w in new_words[j1:j2]
    ]
    delta = len(new_words) - len(old_words)
    phrase = " ".join(added).strip()
    if not phrase:
        return f"Content changed ({delta:+d} words)."
    return f"Added: {phrase[:max_len]}{'…' if len(phrase) > max_len else ''} ({delta:+d} words)."
