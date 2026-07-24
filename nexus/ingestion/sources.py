"""Signal sources and the built-in signal library.

A ``SignalSource`` yields ``RawSignal`` objects for an account. The ``IngestionService``
normalizes, dedupes, and persists them. Real 3rd-party sources (G2, web-visitor pixels, CRM)
implement the same interface; ``DemoSignalSource`` keeps the system runnable with no network and
``WebNewsSource`` shows a live source built on the browser provider.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from datetime import datetime

from nexus.core.db import utcnow
from nexus.models.account import Account

# Name tokens too generic to identify an account in a news headline.
_GENERIC_NAME_TOKENS = frozenset({
    "inc", "llc", "ltd", "corp", "the", "and", "co", "company", "group", "holdings",
    "technologies", "technology", "tech", "solutions", "systems", "global",
    "international", "services", "labs", "io", "app", "ai",
})

# Headlines that are real buying signals -> (signal kind, strength). First match wins. A headline
# that mentions the account but matches none of these is a low-strength "news" mention (kept for
# the timeline, but on its own it won't create an Inbox task).
_NEWS_PATTERNS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("funding", ("raises", "raised", "funding round", "series a", "series b", "series c",
                 "series d", "seed round", "venture", "valuation", "secures $", "raises $"), 0.85),
    ("hiring", ("appoints", "names new", "hires", "joins as", "new ceo", "new cfo", "new cro",
                "new chief", "promoted to", "is hiring", "headcount"), 0.6),
    ("news", ("acquires", "acquisition", "merger", "partners with", "partnership", "launches",
              "expands", "expansion", "opens new", "ipo", "goes public"), 0.6),
)


def _account_keys(account: Account) -> set[str]:
    """Identifying tokens that must appear in a headline for it to be 'about' this account — the
    main name token(s) plus the domain root. Drops generic corp-suffix words."""
    keys: set[str] = set()
    name = (account.name or "").strip().lower()
    if name:
        keys.add(name)
        for tok in re.split(r"[^a-z0-9]+", name):
            if len(tok) >= 3 and tok not in _GENERIC_NAME_TOKENS:
                keys.add(tok)
    root = (account.domain or "").lower().split(".")[0].strip()
    if len(root) >= 3 and root not in _GENERIC_NAME_TOKENS:
        keys.add(root)
    return keys


def _classify_news(text: str) -> tuple[str, float]:
    low = text.lower()
    for kind, needles, strength in _NEWS_PATTERNS:
        if any(n in low for n in needles):
            return kind, strength
    return "news", 0.4

# Built-in signal library — the catalogue NEXUS ships with out of the box.
SIGNAL_LIBRARY: dict[str, dict] = {
    "funding": {"category": "3rd-party", "default_strength": 0.8, "desc": "Raised a round"},
    "news": {"category": "3rd-party", "default_strength": 0.4, "desc": "Press / announcement"},
    "job_posting": {"category": "3rd-party", "default_strength": 0.6, "desc": "Relevant hiring"},
    "hiring": {"category": "3rd-party", "default_strength": 0.5, "desc": "Headcount growth"},
    "g2_intent": {"category": "3rd-party", "default_strength": 0.9, "desc": "G2 category intent"},
    "tech_install": {"category": "3rd-party", "default_strength": 0.6, "desc": "Adopted a tech"},
    "job_switch": {"category": "3rd-party", "default_strength": 0.8, "desc": "Champion moved"},
    "web_visit": {"category": "1st-party", "default_strength": 0.7, "desc": "Visited our site"},
    "product_usage": {"category": "1st-party", "default_strength": 0.8, "desc": "Used product"},
    "call": {"category": "1st-party", "default_strength": 0.6, "desc": "Call / meeting"},
}


@dataclass(slots=True)
class RawSignal:
    kind: str
    source: str
    title: str
    dedupe_key: str
    body: str | None = None
    url: str | None = None
    strength: float | None = None  # falls back to library default
    occurred_at: datetime = field(default_factory=utcnow)
    contact_id: str | None = None

    def resolved_strength(self) -> float:
        if self.strength is not None:
            return self.strength
        return SIGNAL_LIBRARY.get(self.kind, {}).get("default_strength", 0.5)


class SignalSource(abc.ABC):
    name: str

    @abc.abstractmethod
    async def fetch(self, account: Account) -> list[RawSignal]: ...


class DemoSignalSource(SignalSource):
    """Deterministic synthetic signals so the pipeline runs end-to-end without network."""

    name = "demo"

    async def fetch(self, account: Account) -> list[RawSignal]:
        out: list[RawSignal] = []
        domain = account.domain or account.name.lower().replace(" ", "")
        if (account.employee_count or 0) >= 100:
            out.append(
                RawSignal(
                    kind="funding",
                    source=self.name,
                    title=f"{account.name} announced new funding",
                    dedupe_key=f"funding:{domain}",
                    strength=0.8,
                )
            )
        out.append(
            RawSignal(
                kind="job_posting",
                source=self.name,
                title=f"{account.name} is hiring in a relevant function",
                dedupe_key=f"job_posting:{domain}",
            )
        )
        return out


def _parse_feed(xml_text: str) -> list[dict]:
    """Parse an RSS or Atom feed into ``[{title, link, summary}]``. Namespace-tolerant, never
    raises. Handles RSS ``<item>`` (link is text) and Atom ``<entry>`` (link is an href attr)."""
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    items: list[dict] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        for child in el:
            lt = _local(child.tag)
            if lt == "title" and child.text:
                d["title"] = child.text.strip()
            elif lt == "link":
                href = child.get("href")
                link = (href or child.text or "").strip()
                if link and "link" not in d:
                    d["link"] = link
            elif lt in ("description", "summary", "content") and child.text and "summary" not in d:
                d["summary"] = child.text.strip()
        if d.get("title"):
            items.append(d)
    return items


class RssSignalSource(SignalSource):
    """Live source: read a company's own RSS/Atom feed (blog / newsroom / press releases).

    The feed URL comes from ``account.custom_fields['rss_feed']`` when set, else common conventions
    on the account domain. Entries are the company's *own* posts, so — unlike open-web search —
    they need no name-matching. OFF by default: activated by adding ``rss`` to
    ``NEXUS_SIGNAL_SOURCES``. The HTTP fetch is injectable so the parser runs offline with canned
    feed XML. Never raises across the boundary.
    """

    name = "rss"

    def __init__(self, fetch=None, *, max_items: int = 5) -> None:
        self._fetch = fetch  # async (url) -> str | None ; None = real httpx
        self._max = max_items

    async def _http_get(self, url: str) -> str | None:
        if self._fetch is not None:
            return await self._fetch(url)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "NexusGTM/1.0 (+signals)"})
                if resp.status_code == 200 and resp.text:
                    return resp.text
        except Exception:  # a flaky feed host must never break ingestion
            return None
        return None

    def _feed_urls(self, account: Account) -> list[str]:
        cf = getattr(account, "custom_fields", None) or {}
        explicit = str(cf.get("rss_feed") or "").strip()
        if explicit:
            return [explicit]
        domain = (account.domain or "").strip().lower().lstrip("@")
        if not domain:
            return []
        return [
            f"https://{domain}/feed",
            f"https://{domain}/rss",
            f"https://{domain}/blog/rss.xml",
            f"https://{domain}/news/rss",
        ]

    async def fetch(self, account: Account) -> list[RawSignal]:
        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")
        for url in self._feed_urls(account):
            xml = await self._http_get(url)
            if not xml:
                continue
            items = _parse_feed(xml)
            if not items:
                continue
            out: list[RawSignal] = []
            for it in items[: self._max]:
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                link = (it.get("link") or "").strip()
                summary = (it.get("summary") or "").strip()
                kind, strength = _classify_news(f"{title} {summary}")
                out.append(
                    RawSignal(
                        kind=kind,
                        source=self.name,
                        title=title[:380],
                        body=summary or None,
                        url=link or None,
                        strength=strength,
                        dedupe_key=f"rss:{anchor}:{link or title}"[:200],
                    )
                )
            if out:
                return out
        return []


class WebNewsSource(SignalSource):
    """Live source: searches the web for recent news about the account."""

    name = "web_news"

    def __init__(self, browser):
        self.browser = browser

    async def fetch(self, account: Account) -> list[RawSignal]:
        name = (account.name or "").strip()
        if not name:
            return []
        keys = _account_keys(account)
        if not keys:
            return []
        # Bias the query toward buying-event news, and include the industry so a generic name
        # ("Bill", "Increase") doesn't pull unrelated results.
        industry = (account.industry or "").strip()
        query = f'{name} {industry} (funding OR hiring OR launches OR partnership OR acquisition)'
        try:
            hits = await self.browser.search(query.strip(), limit=6) or []
        except Exception:  # a flaky search must not break ingestion
            hits = []

        out: list[RawSignal] = []
        seen: set[str] = set()
        now = utcnow()
        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")
        for h in hits:
            title = (h.get("title") or "").strip()
            snippet = (h.get("snippet") or "").strip()
            url = h.get("url") or ""
            hay = f"{title} {snippet}".lower()
            # The result must actually NAME this account, else it's a generic news-site index
            # ("Banking News and Analysis | Banking Dive") — the exact junk that polluted the inbox.
            if not any(k in hay for k in keys):
                continue
            if not title:
                continue
            kind, strength = _classify_news(f"{title} {snippet}")
            # Event-bucketed dedupe, NOT per-URL: the same funding round gets re-covered by many
            # outlets under fresh URLs for weeks, and per-URL keys re-alerted completed accounts on
            # every refresh (observed: 9 distinct "funding" URLs for one account in two weeks). One
            # event-class alert per account per window: funding/hiring monthly, other news weekly.
            # The URL still lands on the signal itself for the timeline.
            if kind in ("funding", "hiring"):
                dedupe_key = f"{kind}:{anchor}:{now:%Y-%m}"
            else:
                # Separate buckets for real events (acquires/partners/launches, >=0.5) vs weak
                # mentions, so a Monday press mention can't shadow a Wednesday acquisition.
                iso = now.isocalendar()
                tier = "evt" if strength >= 0.5 else "mention"
                dedupe_key = f"news:{anchor}:{iso[0]}-W{iso[1]:02d}:{tier}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(
                RawSignal(
                    kind=kind,
                    source=self.name,
                    title=title[:380],
                    body=snippet or None,
                    url=url,
                    strength=strength,
                    dedupe_key=dedupe_key,
                )
            )
        return out[:4]
