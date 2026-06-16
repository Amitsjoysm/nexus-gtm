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
        for h in hits:
            title = (h.get("title") or "").strip()
            snippet = (h.get("snippet") or "").strip()
            url = h.get("url") or ""
            hay = f"{title} {snippet}".lower()
            # The result must actually NAME this account, else it's a generic news-site index
            # ("Banking News and Analysis | Banking Dive") — the exact junk that polluted the inbox.
            if not any(k in hay for k in keys):
                continue
            dedupe_key = f"news:{url or title}"
            if not title or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            kind, strength = _classify_news(f"{title} {snippet}")
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
