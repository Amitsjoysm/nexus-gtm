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


# ---- strict shapes, for a value somebody typed, uploaded or a model returned (2026-10-02) ----------
#
# `canonical_profile_url` FINDS a profile inside a string, which is right for search results and
# wrong for a field: "https://evil.io/?next=https://linkedin.com/in/x" contains one. A field holds a
# person's profile only if the WHOLE value is one; anything else is refused, never coerced.

_HOST = r"(?:https?://)?(?:[a-z0-9-]{1,10}\.)?linkedin\.com"
_STRICT_PROFILE_RE = re.compile(
    rf"^{_HOST}/in/([^/?#\s]{{2,200}})(?:/[^?#\s]*)?/?(?:[?#]\S*)?$", re.IGNORECASE)
_STRICT_COMPANY_RE = re.compile(
    rf"^{_HOST}/(company|showcase|school)/([^/?#\s]{{1,200}})(?:/[^?#\s]*)?/?(?:[?#]\S*)?$",
    re.IGNORECASE)


def person_profile_url(value: str | None) -> str | None:
    """``value`` when the whole of it is a LinkedIn member profile URL, else None.

    The value comes back as given (trimmed): dedupe and the shared stores compare through
    `people.store.normalise_linkedin`, so there is no need to rewrite what a person typed."""
    raw = (value or "").strip()
    return raw if raw and _STRICT_PROFILE_RE.match(raw) else None


def company_page_url(value: str | None) -> str | None:
    """The canonical ``https://www.linkedin.com/<company|showcase|school>/<slug>`` for a value that is
    one, else None. A person's profile is not a company page."""
    m = _STRICT_COMPANY_RE.match((value or "").strip())
    if not m:
        return None
    return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2)}"


_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")


def is_own_profile(hit, full_name: str) -> bool:
    """Whether this LinkedIn result is the named person's OWN profile.

    A LinkedIn profile result is titled with its owner's name ("Jane Doe - VP Sales - Acme |
    LinkedIn"), and most slugs carry it ("jane-doe-4a1"). The snippet does not count: a colleague's
    profile that mentions Jane in its "about" text names her and her company, and attaching it to
    her is the wrong-person failure `contacts/affiliation.py` exists to prevent."""
    tokens = _name_tokens(full_name)
    url = canonical_profile_url(_hit_field(hit, "url"))
    if not tokens or not url:
        return False
    slug = url.rsplit("/in/", 1)[-1].lower()
    lead = _TITLE_SPLIT.split(_hit_field(hit, "title").strip(), maxsplit=1)[0].lower()
    surname, given = tokens[-1], tokens[0]

    def names(text: str) -> bool:
        return surname in text and (given in text or len(tokens) == 1)

    return names(slug) or names(lead)


def _hit_field(hit, name: str) -> str:
    if isinstance(hit, dict):
        return str(hit.get(name) or "")
    return str(getattr(hit, name, "") or "")


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
            match = self._best_match(hits, tokens, account)
            if match:
                return match
        return None

    @staticmethod
    def _best_match(hits, tokens: list[str], account: Account | None = None) -> str | None:
        from nexus.contacts.affiliation import proves_affiliation

        name = " ".join(tokens)
        for hit in hits:
            canon = canonical_profile_url(_hit_field(hit, "url"))
            if not canon:
                continue
            # The profile must be THEIRS: named in its title or slug, not merely in its snippet
            # (a colleague's profile mentions them), and for multi-token names the given name too,
            # so "…/in/john-collison" isn't matched for "Jane Collison".
            if not is_own_profile(hit, name):
                continue
            # A NAME does not tell one Furqan Mustafa from another — the staging report was
            # exactly a namesake's profile. The profile must name THIS company too.
            if account is not None and not proves_affiliation(hit, account):
                continue
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
