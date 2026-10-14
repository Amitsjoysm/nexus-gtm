# nexus/companies/resolution.py
"""Which shared company row an account belongs to.

Identity is the **normalised domain** and nothing else. That is a deliberate narrowing: name-based
resolution across tenants is how one workspace's data leaks into another's, and this codebase has
already shipped five wrong-attribution bugs in the signal subsystem by trusting a name match.

An account with no domain therefore has **no** company and keeps being crawled per-tenant. That is
not a gap to close later — it is the safety property.
"""
from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger("nexus.companies.resolution")

# Hosts that are never one company: free mail, link shorteners, and the reserved names people put
# in test data. Resolving on these would merge unrelated businesses into one shared record.
_NON_COMPANY_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "icloud.com", "proton.me", "protonmail.com", "mail.com", "gmx.com",
    "example.com", "example.org", "example.net", "test.com", "localhost",
    "bit.ly", "linktr.ee",
})

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def normalise_domain(value: str | None) -> str:
    """A comparable domain, or "" when there is nothing usable.

    Strips scheme, credentials, path, port and a leading ``www.``. Returns "" rather than raising —
    a malformed domain must not break ingestion for the account that carries it.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    raw = _SCHEME.sub("", raw)
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.rsplit("@", 1)[-1]          # an email was passed instead of a domain
    raw = raw.split(":", 1)[0]            # port
    raw = raw.strip(". ")
    if raw.startswith("www."):
        raw = raw[4:]
    # A bare label ("acme") is not a domain; requiring a dot keeps hostnames and typos out.
    if "." not in raw or " " in raw:
        return ""
    if raw in _NON_COMPANY_DOMAINS:
        return ""
    return raw


def company_id_for(domain: str) -> str:
    """Deterministic id for a normalised domain.

    Deterministic so two workers racing to resolve the same company generate the same primary key —
    one insert wins, the other hits the unique constraint and re-reads, instead of both succeeding
    and creating a duplicate that silently splits the timeline in two.
    """
    return hashlib.sha1(domain.encode("utf-8")).hexdigest()


async def resolve_company(session, *, domain: str | None, name: str = "", **fields):
    """Find or create the shared company for a domain. Returns None when there is no usable domain.

    Uses the platform session the caller supplies — the table is global, so a TenantSession would be
    the wrong scope and, under RLS, would silently see nothing.

    Firmographic fields only ever FILL BLANKS on an existing row. A tenant's correction must not
    rewrite what every other tenant sees; per-tenant overrides belong on ``accounts``.
    """
    from nexus.models.company import Company

    normalised = normalise_domain(domain)
    if not normalised:
        return None

    source = fields.pop("source", "crawl")
    cid = company_id_for(normalised)
    company = await session.get(Company, cid)
    if company is None:
        company = Company(
            id=cid, domain=normalised, name=(name or normalised).strip(), source=source,
        )
        session.add(company)

    for key in ("industry", "country", "employee_count"):
        value = fields.get(key)
        if value and not getattr(company, key, None):
            setattr(company, key, value)
    # Union rather than overwrite: one tenant seeing "Postgres" must not erase another's "Kafka".
    stack = fields.get("tech_stack")
    if stack:
        merged = list(dict.fromkeys([*(company.tech_stack or []), *stack]))
        company.tech_stack = merged
    if name and not company.name:
        company.name = name.strip()
    if source != "linkedin":
        # A LinkedIn write sets these columns itself, from LinkedIn, right after this returns.
        await derive_codes(session, company)
    return company


# ---- codes derived from a row's own text (2026-10-02) ----------------------------------------------
#
# The ICP chain's first step (`prospecting/companies.py`) finds shared companies by LinkedIn's
# industry code, office countries and size band. Only a LinkedIn read set those, so a company that
# arrived any other way (an upload, a CRM pull, a hand-add, a web find) could never be offered to
# another workspace: 256 of 393 shared rows on the local deploy. Decided with the product owner:
# derive them from the row's own industry, country and headcount, for free, with the deterministic
# mapper the relevance engine already scores with (never the LLM). `linkedin_fetched_at` stays NULL,
# which is what tells the chain the codes were read off words rather than a LinkedIn filter, and
# keeps those rows behind the workspace's fit threshold.

def derived_industry_id(text: str | None) -> int | None:
    """The first LinkedIn industry code a free-text industry maps to, or None."""
    from nexus.prospecting.industries import match_term, split_terms

    for term in split_terms([text]) if text else []:
        ids = match_term(term)
        if ids:
            return int(ids[0])
    return None


def derived_country_code(text: str | None) -> str | None:
    """An ISO code for a country name, or None when the name is not a known country."""
    from nexus.contacts.phone import region_for
    from nexus.prospecting.companies import country_label

    code = (region_for(text or "") or "").upper()
    code = {"UK": "GB"}.get(code, code)
    # `country_label` echoes an unknown code back; a known one comes back as a name.
    return code if len(code) == 2 and country_label(code) != code else None


async def derive_codes(session, company) -> None:
    """Fill the blank code columns of a row LinkedIn has not read. Never overwrites anything."""
    from nexus.models.company import CompanyCountry

    if company.linkedin_fetched_at is not None:
        return
    if company.linkedin_industry_id is None:
        company.linkedin_industry_id = derived_industry_id(company.industry)
    if company.hq_country_code is None:
        company.hq_country_code = derived_country_code(company.country)
    if (company.employee_range_min is None and company.employee_range_max is None
            and company.employee_count):
        company.employee_range_min = company.employee_range_max = int(company.employee_count)
    code = company.hq_country_code
    if code and await session.get(CompanyCountry, (company.id, code)) is None:
        session.add(CompanyCountry(company_id=company.id, country_code=code))
