"""Turn a search-results page into hits.

Lives in the main package rather than in the service so parsing is covered by the offline suite:
markup changes are the thing most likely to break silently, and a parser that only runs on the VM is
a parser nobody tests. `tests/fixtures/serp_ddg.html` is a real page, so a layout change shows up as
a failing test rather than as weeks of "no signals".

`BlockedByEngine` is deliberately not an empty list. "The engine refused" and "the market is quiet"
must never look alike — the distinction `signal_source_runs` draws between `error` and `empty`, and
the reason a source that finds nothing forever is detectable at all.
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlsplit

_ANCHOR = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<text>.*?)</a>", re.DOTALL | re.IGNORECASE)
_HREF = re.compile(r'href="(?P<href>[^"]*)"', re.IGNORECASE)
_CLASS = re.compile(r'class="(?P<cls>[^"]*)"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
#: Phrases the HTML endpoint serves instead of results once it has decided we are a bot.
_BLOCK_MARKERS = ("bots use duckduckgo too", "unusual traffic", "anomaly-modal")

SOURCE = "nexusfetch:ddg"


class BlockedByEngine(RuntimeError):
    """The engine served an anti-bot page. Not the same as finding nothing."""


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", fragment)).split())


def _destination(href: str) -> str:
    """The page a result actually points at, with DuckDuckGo's `/l/?uddg=` redirect removed.

    The href arrives HTML-escaped (`&amp;rut=`), so it is unescaped BEFORE the query string is
    parsed — otherwise `amp;rut` becomes a key and the split happens in the wrong place.
    """
    raw = html.unescape(href or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parts = urlsplit(raw)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parts.query).get("uddg")
        if target:
            return unquote(target[0])
    return raw


def _is_engine_url(url: str) -> bool:
    # Sponsored results go through DuckDuckGo's own tracker (`/y.js`), not a `uddg=` redirect, so
    # they still point at the engine after unwrapping. An ad is not an event about the account.
    host = urlsplit(url).hostname or ""
    return host.endswith("duckduckgo.com")


def parse_ddg(body: str, *, limit: int = 5) -> list[dict]:
    """Hits from a DuckDuckGo HTML results page, in page order.

    Titles and snippets are paired by the page they point at rather than by position, so a result
    with no snippet — or an ad with one — cannot shift every later snippet onto the wrong title.
    """
    titles: list[tuple[str, str]] = []
    snippets: dict[str, str] = {}
    for match in _ANCHOR.finditer(body or ""):
        attrs = match.group("attrs")
        classes = set((_CLASS.search(attrs) or [None, ""])[1].split())
        href = _HREF.search(attrs)
        if not href or not classes & {"result__a", "result__snippet"}:
            continue
        url = _destination(href.group("href"))
        if "result__a" in classes:
            titles.append((url, _text(match.group("text"))))
        else:
            snippets.setdefault(url, _text(match.group("text")))

    if not titles and any(marker in (body or "").lower() for marker in _BLOCK_MARKERS):
        raise BlockedByEngine("duckduckgo served an anti-bot page")

    hits: list[dict] = []
    for url, title in titles:
        if not title or not url.startswith("http") or _is_engine_url(url):
            continue
        hits.append({"title": title, "url": url, "snippet": snippets.get(url, ""), "source": SOURCE})
        if len(hits) >= limit:
            break
    return hits
