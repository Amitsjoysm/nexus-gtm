"""Find a contact's LinkedIn profile URL via web search (Exa).

Contacts sourced by the pattern/stub paths carry no LinkedIn URL, and even the search-backed
people finder only captures one when it happens to sit in a result snippet. This module closes
that gap: given a person + their company, it runs a targeted web search and returns the first
result whose URL is a real ``linkedin.com/in/…`` profile that plausibly matches the name.

It is grounding-only — it never fabricates a URL. Offline (stub search) yields nothing, so the
demo/test path is unchanged. It never raises across the boundary: any failure returns ``None``.
"""
from __future__ import annotations

import logging
import re

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.enrichment.linkedin")

# A public LinkedIn *member* profile URL (…/in/<slug>). Excludes /company/, /school/, /posts/.
_PROFILE_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?linkedin\.com/in/[A-Za-z0-9%_\-]+", re.IGNORECASE
)


def _name_tokens(full_name: str) -> list[str]:
    """Lower-case alphanumeric name tokens (>= 2 chars), diacritics/punctuation stripped."""
    return [t for t in re.split(r"[^a-z0-9]+", (full_name or "").lower()) if len(t) >= 2]


def canonical_profile_url(url: str) -> str | None:
    """Extract + normalize the ``linkedin.com/in/<slug>`` core of a URL, or None if it isn't one."""
    m = _PROFILE_RE.search(url or "")
    if not m:
        return None
    return m.group(0).rstrip("/")


class LinkedInFinder:
    """Search the web for a person's LinkedIn profile and return the best-matching profile URL."""

    def __init__(self, search) -> None:
        # ``search`` is the registry's SearchProvider-backed callable (Exa in prod, stub offline).
        self._search = search

    async def find(self, account: Account, contact: Contact) -> str | None:
        name = (contact.full_name or "").strip()
        tokens = _name_tokens(name)
        if not name or not tokens:
            return None
        company = (account.name or account.domain or "").strip()
        title = (contact.title or "").strip()
        # Two passes: a tight name+company query, then a role-qualified fallback. The person's
        # surname must appear in the winning hit so we don't grab a namesake's profile.
        queries = [f'{name} {company} LinkedIn'.strip()]
        if title:
            queries.append(f'{name} {title} {company} LinkedIn profile'.strip())
        for query in queries:
            try:
                hits = await self._search(query, limit=6) or []
            except Exception as exc:  # a flaky search must never break enrichment
                logger.warning("linkedin search failed for %r: %r", name, exc)
                hits = []
            match = self._best_match(hits, tokens)
            if match:
                return match
        return None

    @staticmethod
    def _best_match(hits, tokens: list[str]) -> str | None:
        surname = tokens[-1]
        given = tokens[0]
        for hit in hits:
            url = getattr(hit, "url", "") or (hit.get("url", "") if isinstance(hit, dict) else "")
            canon = canonical_profile_url(url)
            if not canon:
                continue
            title = getattr(hit, "title", "") or (hit.get("title", "") if isinstance(hit, dict) else "")
            snippet = getattr(hit, "snippet", "") or (
                hit.get("snippet", "") if isinstance(hit, dict) else ""
            )
            hay = f"{canon} {title} {snippet}".lower()
            # Require the surname (in slug/title/snippet); for multi-token names also the given
            # name, so "…/in/john-collison" isn't matched for "Jane Collison".
            if surname in hay and (given in hay or len(tokens) == 1):
                return canon
        return None


async def enrich_contact_linkedin(ts: TenantSession, contact: Contact) -> str | None:
    """Fill ``contact.linkedin_url`` from web search when it's blank. Blank-only: an existing URL
    (from CSV/CRM/people-search) is never overwritten. Returns the URL when newly filled, else None.
    """
    if (contact.linkedin_url or "").strip():
        return None
    account = await ts.get(Account, contact.account_id)
    if account is None:
        return None
    from nexus.integrations.registry import get_registry

    finder = LinkedInFinder(get_registry().search)
    url = await finder.find(account, contact)
    if url:
        contact.linkedin_url = url
    return url
