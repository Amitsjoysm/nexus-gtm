"""Buyers at an account, from its own LinkedIn page: the first answer to Find contacts.

Decided with the product owner 2026-09-24: LinkedIn first, web search only when this finds nobody;
only the titles the ICP names, never "all employees"; the email from our own verified finder.

Three gates, each from the one real run (asked about ``linkedin.com/company/vanta``, the actor
returned a Fleet Coordinator at Flex E Lease):

* **The page must be the account's own** (``similar.verified_page``): its website is the account's
  domain. That URL was a chauffeur firm.
* **Every row must name this company and fit an ICP title** (``linkedin.employee_fits``). The actor
  is scoped to one page, so a row naming another company leaked in from somewhere else.
* **No duplicates**: a person already on the account, by LinkedIn profile or by name, and the same
  person twice in one answer, are dropped.

One employees run per click: $0.02 to start plus $0.003 a profile, short mode (no email search).

**The shared pool answers first** (2026-10-02, `nexus/people/pool.py`): people any workspace's run
already proved work here and fit, seen within the last 180 days, are offered through the same title
gates before a run is bought, and a run is asked only for the rest. Every row a run proves is queued
for the pool, including the ones this workspace did not take.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from nexus.prospecting import linkedin as li

logger = logging.getLogger("nexus.prospecting.contacts")

#: Profiles one run may return. Asked for twice what is wanted, because the gates drop some.
MAX_PROFILES = 25
#: Titles sent to the actor; more only widen the search the gates then have to narrow again.
MAX_TITLES = 10


@dataclass
class ContactsResult:
    people: list[li.LinkedInPerson] = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)
    notes: dict[str, str] = field(default_factory=dict)
    #: Profiles (normalised) that came from the shared pool rather than a LinkedIn read now.
    pooled: set[str] = field(default_factory=set)


def _name_key(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


#: A word that names a rung, not a role. On its own it asks LinkedIn for every VP at the company.
_LEVEL_ONLY = frozenset({"vp", "svp", "evp", "avp", "vice president", "head", "director",
                         "manager", "lead", "chief", "senior", "sr", "executive"})
_ACRONYM = re.compile(r"^[A-Z][A-Za-z&]{1,6}$")


def _variants(title: str) -> list[str]:
    """A title as LinkedIn's title filter can match it.

    "Chief Revenue Officer (CRO)" is two: the words and the acronym a headline uses. "Founder / CEO"
    is two roles. Sent as typed, the brackets and the slash are part of the phrase, which no
    profile holds."""
    text = " ".join(str(title or "").split())
    out: list[str] = []
    for bracketed in re.findall(r"\(([^)]*)\)", text):
        if _ACRONYM.match(bracketed.strip()):
            out.append(bracketed.strip())
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text).strip()
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s+\bor\b\s+|\s*\|\s*", text) if p.strip()]
    return [p for p in parts if p.lower() not in _LEVEL_ONLY] + out


def linkedin_search_titles(icp: dict) -> list[str]:
    """The ``jobTitles`` the employees actor is asked for: the ICP's buyer titles as a person holds
    them, then its level-and-keyword phrasings ("Director of Facilities"). Never a bare level code:
    levels are a gate on what comes back (`lookalike.contacts.title_fits`), not a title."""
    from nexus.relevance.job_levels import expand_titles, spec_from_icp

    icp = icp or {}
    raw: list[str] = []
    for key in ("buyer_titles", "titles"):
        values = icp.get(key) or []
        raw.extend([values] if isinstance(values, str) else list(values))
    titles: list[str] = []
    for title in raw:
        for variant in _variants(title):
            if variant.lower() not in {t.lower() for t in titles}:
                titles.append(variant)
    spec = spec_from_icp(icp)
    if spec.get("title_keywords"):
        titles += [t for t in expand_titles(spec) if t.lower() not in {x.lower() for x in titles}]
    return titles[:MAX_TITLES]


async def linkedin_contacts(ts, account, icp: dict, *, limit: int, client=None,
                            pool: bool = True, read: bool = True,
                            seen: tuple[set[str], set[str]] | None = None) -> ContactsResult:
    """Up to ``limit`` people at the account who fit the ICP's titles. Never raises for LinkedIn
    being unavailable; that is a note, and the caller falls back to web search.

    ``pool`` asks the shared pool, ``read`` buys a LinkedIn read for what is left; Find contacts
    asks InfoJoy between the two (2026-10-26). ``seen`` is ``(profile urls, name keys)`` already
    taken by an earlier step, added to what the account holds."""
    from nexus.integrations.apify import ApifyError, ApifyNotConfigured
    from nexus.lookalike.contacts import icp_title_targets
    from nexus.models.account import Contact
    from nexus.people.store import normalise_linkedin
    from nexus.prospecting.similar import verified_page

    result = ContactsResult()
    targets = icp_title_targets(icp or {})
    titles = linkedin_search_titles(icp or {})
    if not titles:
        # Without titles the only request possible is "everyone who works there".
        result.notes["linkedin"] = "no_titles"
        return result

    existing = await ts.list(Contact, Contact.account_id == account.id)
    urls = {normalise_linkedin(c.linkedin_url) for c in existing} - {""}
    names = {_name_key(c.full_name) for c in existing} - {""}
    if seen is not None:
        urls |= seen[0] - {""}
        names |= seen[1] - {""}

    # People another workspace's LinkedIn read already found here, before buying a run
    # (`nexus/people/pool.py`, 2026-10-02). Same title gates, same duplicate rules.
    from nexus.companies.resolution import normalise_domain
    from nexus.people.pool import enqueue_pool, linkedin_fact, pooled_people_at

    domain = normalise_domain(account.domain)
    if pool:
        for person in await pooled_people_at(domain, targets, limit=limit * 2,
                                             company_name=account.name or domain):
            url, name = normalise_linkedin(person.linkedin_url), _name_key(person.full_name)
            if (url and url in urls) or (name and name in names):
                continue
            if len(result.people) >= limit:
                break
            urls.add(url)
            names.add(name)
            result.people.append(person)
            result.pooled.add(url)
    if len(result.people) >= limit or not read:
        return result

    want = limit - len(result.people)
    try:
        page, reason = await verified_page(account, client=client)
        if page is None:
            result.notes["linkedin"] = reason
            return result
        found = await li.company_employees(
            page.url, titles, limit=min(MAX_PROFILES, max(want * 2, 5)), client=client,
        )
    except ApifyNotConfigured:
        result.notes["linkedin"] = "not_configured"
        return result
    except ApifyError as exc:
        logger.warning("LinkedIn employees failed for %s: %s", account.domain, exc)
        result.notes["linkedin"] = "failed"
        return result

    proven: list[dict] = []
    for person in found:
        ok, why = li.employee_fits(person, page.names, targets)
        if not ok:
            result.discarded[why] += 1
            continue
        # Proven to work here and to fit: worth keeping for the next workspace whether or not this
        # one takes them (over the limit, or already held here).
        proven.append(linkedin_fact(person, domain))
        url, name = normalise_linkedin(person.linkedin_url), _name_key(person.full_name)
        if (url and url in urls) or (name and name in names):
            result.discarded["duplicate"] += 1
            continue
        if len(result.people) >= limit:
            continue
        urls.add(url)
        names.add(name)
        result.people.append(person)
    await enqueue_pool(ts.tenant_id, people=proven)
    if not result.people:
        result.notes["linkedin"] = "nobody_fits"
    return result
