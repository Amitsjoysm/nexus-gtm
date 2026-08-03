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
    raw = raw.split("/", 1)[0].split("?", 1)[0]
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

    cid = company_id_for(normalised)
    company = await session.get(Company, cid)
    if company is None:
        company = Company(
            id=cid, domain=normalised, name=(name or normalised).strip(),
            source=fields.pop("source", "crawl"),
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
    return company
