"""Similar companies from LinkedIn's own "similar pages", resolved to real domains.

The first answer to Find similar (product owner, 2026-09-24); web search is asked only when this
finds nothing (``nexus/lookalike/service.py``).

1. **The seed's page.** Read from the shared company row, where it was attached under the domain
   its own website gave. Otherwise looked up by name, and used ONLY when that page's website is the
   account's domain: ``linkedin.com/company/vanta`` is a chauffeur firm, and its similar pages are
   limousine companies.
2. **Its similar pages.** Stored with the row when LinkedIn was read; asked for otherwise.
3. **Each similar page's domain.** LinkedIn gives none (0 of 72 observed). Pages already in the
   shared store resolve for free; the rest are looked up in ONE bulk run ($0.004 each) and stored,
   so the next workspace asking about a neighbour gets it free. A page with no website of its own
   is dropped as ``no_website``.

Never raises for LinkedIn being unavailable: that is a note, and the caller falls back.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from nexus.companies.resolution import normalise_domain
from nexus.core.db import get_platform_sessionmaker
from nexus.prospecting import linkedin as li
from nexus.prospecting.companies import store_companies

logger = logging.getLogger("nexus.prospecting.similar")

#: A similar page looked up and found to have no website of its own is remembered this long in the
#: shared ``web_cache``: it can never become an account, so asking again only re-buys the same
#: answer. Websites do get added, hence an expiry rather than forever.
NO_WEBSITE_TTL_S = 30 * 24 * 3600
_CACHE_KIND = "linkedin"


@dataclass
class SimilarResult:
    #: Shared ``Company`` rows, in LinkedIn's order, each with its own domain.
    companies: list = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)
    notes: dict[str, str] = field(default_factory=dict)


def _client_for(client):
    """The client to use; a seam so the lookalike service's tests can supply one."""
    return client


async def _company_by_domain(domain: str):
    from nexus.models.company import Company

    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(Company).where(Company.domain == domain))).first()


async def _domains_for_pages(urls: list[str]) -> dict[str, str]:
    """``{page: domain}`` for similar pages already attached to a shared row."""
    from nexus.models.company import Company

    if not urls:
        return {}
    async with get_platform_sessionmaker()() as s:
        rows = (await s.execute(
            select(Company.linkedin_url, Company.domain).where(Company.linkedin_url.in_(urls))
        )).all()
    return {url: domain for url, domain in rows if url and domain}


async def _companies_for(domains: list[str]) -> dict[str, object]:
    from nexus.models.company import Company

    if not domains:
        return {}
    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(Company).where(Company.domain.in_(domains)))).all()
    return {r.domain: r for r in rows}


async def _link_account(ts, account, company_id: str | None) -> None:
    if company_id and not account.company_id:
        account.company_id = company_id
        await ts.flush()


async def similar_companies(ts, account, *, limit: int, client=None) -> SimilarResult:
    from nexus.integrations.apify import ApifyError, ApifyNotConfigured

    result = SimilarResult()
    domain = normalise_domain(account.domain)
    if not domain:
        result.notes["linkedin"] = "no_domain"
        return result
    client = _client_for(client)
    try:
        company = await _company_by_domain(domain)
        page = company.linkedin_url if company is not None else None
        similar = list(company.similar_linkedin or []) if company is not None else []

        if not page:
            found = await li.find_company_page(account.name or domain, client=client)
            if found is not None:
                # Stored whoever it turns out to be: it is a real company under its own domain.
                await store_companies([found])
            if not li.page_is_for(found, domain):
                result.notes["linkedin"] = "no_verified_page"
                return result
            page, similar = found.linkedin_url, list(found.similar)
            company = await _company_by_domain(domain)

        link_to = company.id if company is not None else None

        if not similar:
            details = (await li.company_details([page], client=client)).get(page)
            if details is not None and li.page_is_for(details, domain):
                await store_companies([details])
                similar = list(details.similar)
        if not similar:
            result.notes["linkedin"] = "no_similar"
            return result

        urls = [s["linkedin_url"] for s in similar if s.get("linkedin_url")]
        domains = await _domains_for_pages(urls)
        known_empty = await _known_without_website([u for u in urls if u not in domains])
        missing = [u for u in urls if u not in domains and u not in known_empty]
        fetched: dict = {}
        if missing:
            fetched = await li.company_details(missing, client=client)
            await store_companies(list(fetched.values()))
            for url, c in fetched.items():
                if c.domain:
                    domains[url] = c.domain
                else:
                    await _remember_without_website(url)
        rows = await _companies_for(sorted(set(domains.values())))

        seen: set[str] = set()
        for url in urls:
            peer = domains.get(url)
            if not peer:
                # No website of its own (looked up now, or remembered), or the lookup returned
                # nothing for this page.
                reason = "no_website" if url in fetched or url in known_empty else "not_found"
                result.discarded[reason] += 1
            elif peer == domain or peer in seen:
                continue                 # the seed itself, or a second page on one domain
            elif peer in rows and len(result.companies) < limit:
                seen.add(peer)
                result.companies.append(rows[peer])
        # Last, after every shared-store write: linking is a write on the TENANT session, and a
        # tenant write transaction held open across a platform write is the ordering
        # `nexus/people/enrich.py` warns about (it deadlocks outright on SQLite).
        await _link_account(ts, account, link_to)
    except ApifyNotConfigured:
        result.notes["linkedin"] = "not_configured"
    except ApifyError as exc:
        logger.warning("LinkedIn similar companies failed for %s: %s", domain, exc)
        result.notes["linkedin"] = "failed"
    return result


async def _known_without_website(urls: list[str]) -> set[str]:
    from nexus.fetching import cache

    out = set()
    for url in urls:
        if (await cache.get(_CACHE_KIND, url) or {}).get("no_website"):
            out.add(url)
    return out


async def _remember_without_website(url: str) -> None:
    from nexus.fetching import cache

    await cache.put(_CACHE_KIND, url, payload={"no_website": True}, ttl_s=NO_WEBSITE_TTL_S)
