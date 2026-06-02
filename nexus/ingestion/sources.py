"""Signal sources and the built-in signal library.

A ``SignalSource`` yields ``RawSignal`` objects for an account. The ``IngestionService``
normalizes, dedupes, and persists them. Real 3rd-party sources (G2, web-visitor pixels, CRM)
implement the same interface; ``DemoSignalSource`` keeps the system runnable with no network and
``WebNewsSource`` shows a live source built on the browser provider.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from nexus.core.db import utcnow
from nexus.models.account import Account

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
        hits = await self.browser.search(f"{account.name} news", limit=3)
        out: list[RawSignal] = []
        for h in hits:
            url = h.get("url", "")
            out.append(
                RawSignal(
                    kind="news",
                    source=self.name,
                    title=h.get("title", "News")[:380],
                    body=h.get("snippet"),
                    url=url,
                    dedupe_key=f"news:{url or h.get('title','')}",
                )
            )
        return out
