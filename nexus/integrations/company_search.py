# nexus/integrations/company_search.py
"""Company-discovery capability: turn an ICP into net-new company candidates.

This is the seam the :class:`~nexus.agents.discovery.DiscoveryAgent` fills its web gap from.
InfoJoy (highest trust), then web search, then Apify connectors all implement
:class:`CompanySearchProvider`; the :class:`~nexus.integrations.registry.DataSourceRegistry`
runs them in priority order and merges by domain. Adapters never raise across the boundary.

Shipped adapters:
  * :class:`StubCompanySearchProvider` — returns ``[]`` (zero-network default).
  * :class:`SearchBackedCompanySearchProvider` — builds a query from the ICP and derives
    candidates from a :class:`~nexus.integrations.search.SearchProvider`'s results.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from nexus.integrations.search import SearchProvider

logger = logging.getLogger("nexus.integrations.company_search")


@dataclass(slots=True)
class CompanyCandidate:
    """A discovered company. ``confidence`` lets the registry rank/merge across sources."""

    name: str
    domain: str | None = None
    url: str | None = None
    industry: str | None = None
    country: str | None = None
    employee_count: int | None = None
    snippet: str = ""
    source: str = ""
    confidence: float = 0.5
    # Per-field origin, populated by the registry on merge ({field: source}).
    provenance: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "domain": self.domain,
            "url": self.url,
            "industry": self.industry,
            "country": self.country,
            "employee_count": self.employee_count,
            "snippet": self.snippet,
            "source": self.source,
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
        }


def domain_from_url(url: str | None) -> str | None:
    """Normalize a URL to a bare registrable host (drops scheme, ``www.``, path)."""
    if not url:
        return None
    netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]  # strip creds / port
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def icp_to_query(icp: dict) -> str:
    """Build a web-search query from the conversation ICP (kept stable for caching)."""
    parts: list[str] = []
    if icp.get("industries"):
        parts.append(" ".join(map(str, icp["industries"])))
    parts.append("companies")
    if icp.get("geo"):
        parts.append("in " + " ".join(map(str, icp["geo"])))
    if icp.get("intent_signals"):
        parts.append(" ".join(map(str, icp["intent_signals"])))
    return " ".join(parts)


class CompanySearchProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def search(self, icp: dict, *, limit: int = 25) -> list[CompanyCandidate]: ...


class StubCompanySearchProvider(CompanySearchProvider):
    """Deterministic offline default: no network, no candidates."""

    name = "stub"

    async def search(self, icp: dict, *, limit: int = 25) -> list[CompanyCandidate]:
        return []


class SearchBackedCompanySearchProvider(CompanySearchProvider):
    """Derive company candidates from a general web search of the ICP.

    Low-to-moderate confidence: a SERP hit is a hint, not a verified firmographic record, so
    InfoJoy/Apify (when present) outrank these in the registry merge.
    """

    name = "search"

    def __init__(self, search: SearchProvider, *, confidence: float = 0.4):
        self.search_provider = search
        self.confidence = confidence

    async def search(self, icp: dict, *, limit: int = 25) -> list[CompanyCandidate]:
        query = icp_to_query(icp)
        try:
            hits = await self.search_provider.search(query, limit=limit)
        except Exception as exc:  # provider isolation
            logger.warning("company search via %s failed: %r", self.name, exc)
            return []

        industry = (icp.get("industries") or [None])[0]
        country = (icp.get("geo") or [None])[0]
        out: list[CompanyCandidate] = []
        for hit in hits:
            domain = domain_from_url(getattr(hit, "url", None))
            if not domain:
                continue
            out.append(
                CompanyCandidate(
                    name=(getattr(hit, "title", None) or domain).strip(),
                    domain=domain,
                    url=getattr(hit, "url", None),
                    industry=industry,
                    country=country,
                    snippet=getattr(hit, "snippet", "") or "",
                    source=self.name,
                    confidence=self.confidence,
                )
            )
        return out
