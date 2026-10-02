"""Find a company's own website domain from its name.

"Find similar people" returns an employer as plain text — the parsed LinkedIn headline — and nothing
resolved it to a domain, so filing that person under a new account left the rep typing the domain by
hand. Blank meant no email could be found and no signals were ever collected for that company.

**A name is not an identity**, which is the rule the shared company store enforces and the reason
this returns evidence rather than a bare string: the search hit's title and URL travel back so the
rep sees what it matched. It only ever PREFILLS a form a person confirms — nothing files an account
under its answer unseen, because a name match cannot say which "Globex" was meant (measured live:
"Globex" resolves to a login page at ``globex.international``).

A host is offered when its name IS the company's ("Acme Corp" -> ``acme.com``, ``acme.io``) or when
its page title carries the full company name ("Acme Corp" -> ``acmehq.com``). A near miss like
``acmeplumbing.com`` matches neither and is not offered.

Directory, social and news hosts are excluded through the same `_NON_COMPANY_HOSTS` list company
discovery uses: a company's LinkedIn page or Crunchbase profile is never its domain.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("nexus.enrichment.company_domain")

#: Legal-form words that are not part of how a company names its website.
_SUFFIXES = frozenset({
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.", "corporation", "co", "co.",
    "company", "gmbh", "bv", "nv", "sa", "srl", "plc", "pvt", "private", "pty", "ag", "ab", "oy",
    "as", "aps", "kk", "sas", "sl", "spa", "group", "holdings", "technologies", "labs",
})
_WORD = re.compile(r"[a-z0-9]+")


def _normalise_name(name: str) -> str:
    """"Acme Corp." -> "acme"; "The Acme Group" -> "acme"."""
    words = [w for w in _WORD.findall((name or "").lower()) if w not in _SUFFIXES and w != "the"]
    return "".join(words)


def _host_root(domain: str) -> str:
    parts = (domain or "").lower().split(".")
    if len(parts) < 2:
        return ""
    # acme.co.uk -> acme; acme.com -> acme
    return parts[-3] if len(parts) > 2 and len(parts[-2]) <= 3 else parts[-2]


@dataclass(frozen=True)
class CompanyDomain:
    """A resolved domain and the search result that proved it, so a person can check the match."""

    domain: str
    url: str = ""
    title: str = ""


def _acceptable(name: str, host: str, title: str) -> bool:
    from nexus.integrations.company_search import _NON_COMPANY_HOSTS

    if not host or any(host == bad or host.endswith("." + bad) for bad in _NON_COMPANY_HOSTS):
        return False
    wanted = _normalise_name(name)
    if not wanted:
        return False
    root = _normalise_name(_host_root(host))
    if root == wanted:
        return True
    # A person confirms what this offers, so a title that names the company is enough.
    return (name or "").strip().lower() in (title or "").lower()


async def resolve_company_domain(name: str, *, search=None) -> CompanyDomain | None:
    """The company's own website domain, or ``None``. Never raises."""
    company = (name or "").strip()
    if not company:
        return None
    try:
        if search is None:
            from nexus.integrations.search.provider import get_search_provider

            search = get_search_provider().search
        hits = await search(f"{company} official website", limit=5) or []
    except Exception as exc:  # a flaky search must not break adding the person
        logger.info("company domain search failed for %r: %r", company, exc)
        return None

    from nexus.integrations.company_search import domain_from_url

    for hit in hits:
        url = getattr(hit, "url", None) or (hit.get("url") if isinstance(hit, dict) else "") or ""
        title = getattr(hit, "title", None) or (hit.get("title") if isinstance(hit, dict) else "") or ""
        host = domain_from_url(url) or ""
        if _acceptable(company, host, title):
            return CompanyDomain(domain=host, url=url, title=title)
    return None


async def resolve_from_linkedin_page(url: str, *, client=None) -> CompanyDomain | None:
    """The website domain the company's own LinkedIn page names, or ``None``. Never raises.

    The better evidence when a similar person's current role links the page (2026-10-02): the page
    states the company's website, where a name search has to guess which "Globex" was meant. Read the
    way Find similar reads pages (`prospecting/similar.py`): the shared store first, at no cost; then
    ONE company-details run, stored for every workspace; a page with no website is remembered for
    30 days so it is not bought again. Still only a prefill the rep confirms.
    """
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.prospecting import linkedin as li

    page = li.canonical_company_url(url)
    if not page:
        return None
    try:
        async with get_platform_sessionmaker()() as s:
            row = (await s.execute(
                select(Company.domain, Company.name).where(Company.linkedin_url == page)
            )).first()
        if row is not None and row[0]:
            return CompanyDomain(domain=row[0], url=page, title=row[1] or "")

        from nexus.prospecting.companies import store_companies
        from nexus.prospecting.similar import _known_without_website, _remember_without_website

        if await _known_without_website([page]):
            return None
        details = (await li.company_details([page], client=client)).get(page)
        if details is None:
            return None
        await store_companies([details])
        if not details.domain:
            await _remember_without_website(page)
            return None
        return CompanyDomain(domain=details.domain, url=page, title=details.name or "")
    except Exception as exc:  # a lookup that fails costs the prefill, never the form
        logger.info("company page lookup failed for %s: %r", page, exc)
        return None
