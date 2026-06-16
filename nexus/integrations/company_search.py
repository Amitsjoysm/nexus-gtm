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
import re
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


# Hosts that are never the company we want: search/social, news, job boards, and
# "best-of"/directory aggregators. A SERP for "logistics companies" is full of these, and the
# naive title-as-name extraction was persisting them as real Accounts (e.g. "100 Top Companies
# in Pune | F6S" -> f6s.com). Matched as suffix so subdomains are covered.
_NON_COMPANY_HOSTS: frozenset[str] = frozenset({
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com",
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "reddit.com", "medium.com", "quora.com", "pinterest.com",
    "wikipedia.org", "crunchbase.com", "f6s.com", "g2.com", "capterra.com",
    "glassdoor.com", "indeed.com", "naukri.com", "workindia.in", "ziprecruiter.com",
    "monster.com", "shine.com", "timesjobs.com", "foundit.in",
    "economictimes.indiatimes.com", "indiatimes.com", "forbes.com", "inc.com",
    "businessinsider.com", "techcrunch.com", "yelp.com", "clutch.co",
    "trustpilot.com", "ambitionbox.com", "zaubacorp.com", "tofler.in",
    # Data-vendor / company-profile / app-store sites: a "similar page" to a company's homepage
    # is often its own listing on one of these, not a real competitor.
    "cbinsights.com", "pitchbook.com", "owler.com", "zoominfo.com", "apollo.io",
    "growjo.com", "tracxn.com", "dnb.com", "bloomberg.com", "indexed.vc",
    "apps.apple.com", "play.google.com", "appstore.com", "producthunt.com",
    "wellfound.com", "angel.co", "rocketreach.co", "leadiq.com", "datanyze.com",
})

# Title shapes that betray a listicle / job page / news headline rather than a company name.
# Note the plural list-nouns ("companies"/"startups"): a homepage <title> is the company's own
# name and almost never contains them, whereas a SERP for "fintech companies in the US" returns
# headlines that literally do ("Fintech Companies in United States 2026 | TechList.ai"). The naive
# "number before companies" check missed those, so match the bare plural too.
_NON_COMPANY_NAME_RE = re.compile(
    r"\b(top\s+\d+|best\s+\d+|\d+\s+(top|best|leading|companies|startups|jobs|vacancies)"
    r"|companies|startups|vacanc(y|ies)|hiring|careers?|salaries|reviews?|news"
    r"|jobs?|list of|listicle|director(y|ies)|rankings?)\b",
    re.IGNORECASE,
)

# A four-digit recent year in a title ("... 2026") is a listicle/news tell, never part of a name.
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Title segment separators: a SERP title is often "<core> - <tagline> | <site>". Keep the head.
_TITLE_SPLIT_RE = re.compile(r"\s+[|–—]\s+|\s+-\s+|:\s+")


def clean_company_name(title: str | None) -> str:
    """Reduce a SERP title to its leading segment — the company's own name — dropping the
    "- tagline | SiteName" cruft so a kept candidate is stored as "Acme Robotics", not
    "Acme Robotics - Industrial IoT | acme.io". Splits only on *spaced* separators so hyphenated
    names ("Coca-Cola") survive."""
    if not title:
        return ""
    return (_TITLE_SPLIT_RE.split(title.strip(), maxsplit=1)[0] or "").strip()


def looks_like_company(domain: str | None, name: str | None) -> bool:
    """True when a SERP hit plausibly *is* a company, not an aggregator/listicle/job/news page.

    Deliberately conservative on persistence: a false negative just means a real company is
    missed from the free web tier (a real provider would find it), whereas a false positive
    pollutes the workspace with junk Accounts the rep then has to clean up. ``name`` is expected
    to already be cleaned via :func:`clean_company_name`."""
    if not domain:
        return False
    host = domain.lower().lstrip(".")
    if any(host == bad or host.endswith("." + bad) for bad in _NON_COMPANY_HOSTS):
        return False
    if not name:
        return True  # domain-only candidate; nothing in the name to disqualify it
    if _NON_COMPANY_NAME_RE.search(name) or _YEAR_RE.search(name):
        return False
    # Real company names are short; a 7+ word title is a headline, not a name.
    if len(name.split()) > 6:
        return False
    # A real company name isn't mostly digits.
    if sum(ch.isdigit() for ch in name) > len(name) / 3:
        return False
    return True


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
            name = clean_company_name(getattr(hit, "title", None))
            # Skip aggregators, job boards, news, and listicles — they were being persisted as
            # bogus Accounts. A general web SERP is mostly these; only keep plausible companies.
            if not looks_like_company(domain, name):
                continue
            out.append(
                CompanyCandidate(
                    name=(name or domain).strip(),
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
