"""Enrichment providers: derive email/phone for a contact. Tried in order by the waterfall."""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass

from nexus.enrichment.browser import BrowserProvider
from nexus.models.account import Account, Contact

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE = re.compile(r"(?:\+?\d[\d\-\s().]{7,}\d)")


@dataclass(slots=True)
class EnrichmentResult:
    found: bool = False
    email: str | None = None
    email_confidence: float = 0.0
    phone: str | None = None
    phone_confidence: float = 0.0
    source: str | None = None

    @property
    def best_confidence(self) -> float:
        return max(self.email_confidence, self.phone_confidence)


class EnrichmentProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult: ...


class PatternEmailProvider(EnrichmentProvider):
    """Zero-dependency baseline: guess a corporate email from name + domain. Low confidence."""

    name = "pattern"

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        if not account.domain or not contact.full_name.strip():
            return EnrichmentResult()
        parts = re.split(r"\s+", contact.full_name.strip().lower())
        first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
        domain = account.domain.lower().lstrip("@")
        guess = f"{first}.{last}@{domain}" if last else f"{first}@{domain}"
        return EnrichmentResult(
            found=True, email=guess, email_confidence=0.4, source=self.name
        )


class SearchEnrichmentProvider(EnrichmentProvider):
    """Find email/phone from public web results via the browser provider. Higher confidence."""

    name = "search"

    def __init__(self, browser: BrowserProvider):
        self.browser = browser

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        domain = (account.domain or "").lower().lstrip("@")
        query = f'"{contact.full_name}" {account.name} email contact'
        hits = await self.browser.search(query, limit=6)
        blob = " ".join((h.get("snippet", "") + " " + h.get("title", "")) for h in hits)

        result = EnrichmentResult(source=self.name)
        for em in _EMAIL.findall(blob):
            # Prefer an address on the company domain.
            if domain and em.lower().endswith("@" + domain):
                result.email, result.email_confidence, result.found = em, 0.8, True
                break
            if not result.email:
                result.email, result.email_confidence, result.found = em, 0.55, True
        phones = _PHONE.findall(blob)
        if phones:
            result.phone = re.sub(r"[\s().\-]", "", phones[0])
            result.phone_confidence = 0.5
            result.found = True
        return result
