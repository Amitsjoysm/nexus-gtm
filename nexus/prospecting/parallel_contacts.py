"""People at one account from Parallel's people search, each proven here (2026-10-29).

Find contacts asks the shared pool, InfoJoy, a LinkedIn read, then this step, then web search
(owner decision 2026-10-29). Entity Search is ranked, not verified, so nothing it says is taken on
trust. A person is kept only when:

* the company their record names is THIS company (its name, its name's distinctive word, or its
  domain),
* the title their record gives passes the ICP's `title_fits` AND is one of its buyer titles in
  meaning (`gate.same_meaning_title`, every word of a wanted title present). `title_fits` alone
  accepts one shared word, and measured (2026-10-29) for buyer titles Operations / Procurement /
  Supply Chain / HSE Manager it kept "Manager", "Manager - Tax", "Revenue Manager" and "Office
  Manager & Executive Assistant": a search returning 100 people at a company can afford the exact
  ones, and
* their link is their own LinkedIn profile, or a page on the company's own website.

Measured on the first real answers (2026-10-29): a person's description is a record
(`record_fields`) naming ``Company Name`` and ``Title``, and a ``Position`` (their current role, as
text or as a small JSON object); every link was a LinkedIn profile. A prose description is read
for "<title> at <company>" instead.

Every call counts: one request per account asks for the 100 people its price includes, and every
proven person with a profile goes to the shared pool (`people.pool`, source ``parallel_people``),
not only the few this request takes, so the next workspace at this company pays nothing. Emails
come from our verified finder afterwards, charged as every Find contacts email is.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger("nexus.prospecting.parallel_contacts")

#: Words that follow a title and precede the company: "VP Operations at Acme", "..., Acme".
_JOINERS = r"(?:\bat\b|@|,|\||\s-\s|\s–\s|\s—\s|\bwith\b|\bfor\b)"
_LEAD_IN = re.compile(r"^(?:is\s+|was\s+|works\s+as\s+|currently\s+|serves\s+as\s+)*"
                      r"(?:the\s+|a\s+|an\s+)?", re.I)
_LEGAL = re.compile(r"[,.]?\s+(?:inc|corp|corporation|co|company|llc|ltd|limited|plc|gmbh|lp)\.?$",
                    re.I)


@dataclass
class PeopleStep:
    people: list = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)
    #: found | nobody_fits | no_titles | no_domain | not_configured | out_of_credit | rejected |
    #: unavailable
    note: str = ""
    searches: int = 0
    #: The page each kept person was found on, by full name: the contact's evidence.
    sources: dict = field(default_factory=dict)


def _company_terms(account) -> list[str]:
    from nexus.prospecting.vetting import _GENERIC, _label

    name = _LEGAL.sub("", (account.name or "").strip()).strip()
    terms = [name] if name else []
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", name) if w]
    distinctive = [w for w in words if w.lower() not in _GENERIC and len(w) >= 3]
    if distinctive and distinctive[0] not in terms:
        terms.append(distinctive[0])
    label = _label(account.domain or "")
    if label and len(label) >= 3:
        terms.append(label)
    return [t for t in dict.fromkeys(terms) if t]


def title_from(description: str, person_name: str, terms: list[str]) -> str:
    """The title a description gives this person at this company, or "" when it names neither."""
    text = " ".join((description or "").split())
    for term in terms:
        match = re.search(rf"(?P<title>[^.;•]+?)\s*{_JOINERS}\s*(?:the\s+)?{re.escape(term)}\b",
                          text, re.I)
        if not match:
            continue
        title = match.group("title").strip(" ,|-–—")
        if person_name and title.lower().startswith(person_name.lower()):
            title = title[len(person_name):].strip(" ,|-–—")
        title = _LEAD_IN.sub("", title).strip(" ,|-–—")
        # A clause before the title ("Based in Houston, Ana is ...") is not part of it.
        title = title.rsplit(", ", 1)[-1] if len(title) > 80 else title
        if title and len(title) <= 120:
            return title
    return ""


def _names_company(description: str, terms: list[str]) -> bool:
    text = (description or "").lower()
    return any(re.search(rf"\b{re.escape(t.lower())}\b", text) for t in terms)


def _position(value: str) -> dict:
    """A ``Position`` given as JSON (``{"company_name": ..., "title": ...}``), else ``{}``."""
    value = (value or "").strip()
    if not value.startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def company_and_title(entity, terms: list[str]) -> tuple[str, str]:
    """The company a person works at and their title there, from their record; for prose, the
    whole description and the title it gives at this company."""
    from nexus.integrations.parallel import record_fields

    fields = record_fields(entity.description)
    if not fields:
        return entity.description or "", title_from(entity.description, entity.name, terms)
    position = _position(fields.get("Position", ""))
    headline = fields.get("Headline", "")
    # A headline ("<title> at <company>") answers only what the record's own fields do not.
    company = (fields.get("Company Name") or str(position.get("company_name") or "")
               or (headline if title_from(headline, entity.name, terms) else ""))
    title = (fields.get("Title") or str(position.get("title") or "")).strip()
    if not title and headline:
        title = title_from(headline, entity.name, terms)
    return company, title


def _proof(url: str, domain: str) -> str | None:
    """The person's own profile URL, "" for a page on the company's own site, None for neither."""
    from nexus.companies.resolution import normalise_domain
    from nexus.enrichment.linkedin import person_profile_url

    profile = person_profile_url(url or "")
    if profile:
        return profile
    host = normalise_domain(urlsplit(url or "").hostname or "")
    if host and domain and (host == domain or host.endswith("." + domain)):
        return ""
    return None


def objective_for(account, titles: list[str]) -> str:
    return (f"People who currently work at {account.name} ({account.domain}) as "
            + ", ".join(titles[:12]))


async def parallel_people(ts, account, icp: dict, *, want: int, seen_urls: set[str],
                          seen_names: set[str], client=None) -> PeopleStep:
    from nexus.companies.resolution import normalise_domain
    from nexus.lookalike.contacts import icp_title_targets, title_fits
    from nexus.people.store import normalise_linkedin
    from nexus.prospecting.contacts import _name_key
    from nexus.prospecting.linkedin import LinkedInPerson
    from nexus.relevance.gate import same_meaning_title

    step = PeopleStep()
    domain = normalise_domain(account.domain)
    titles = [str(t).strip() for t in icp.get("buyer_titles") or [] if str(t).strip()]
    if want <= 0:
        step.note = "not_needed"
        return step
    if not domain:
        step.note = "no_domain"
        return step
    if not titles:
        step.note = "no_titles"
        return step
    if client is None:
        from nexus.integrations.parallel import ParallelClient

        client = ParallelClient()

    found = await client.entity_search("people", objective_for(account, titles))
    if not found.ok:
        step.note = found.status if found.status in (
            "not_configured", "out_of_credit", "rejected") else "unavailable"
        return step
    step.searches += 0 if found.cached else 1

    targets = icp_title_targets(icp)
    terms = _company_terms(account)
    proven = []
    for entity in found.entities:
        company, title = company_and_title(entity, terms)
        if not _names_company(company, terms):
            step.discarded["wrong_company"] += 1
            continue
        if (not title or not title_fits(title, targets)
                or not same_meaning_title(title, targets.wanted)):
            step.discarded["wrong_title"] += 1
            continue
        profile = _proof(entity.url, domain)
        if profile is None:
            step.discarded["no_proof"] += 1
            continue
        first, _, last = entity.name.strip().partition(" ")
        person = LinkedInPerson(linkedin_url=profile, first_name=first, last_name=last.strip(),
                                title=title, company_name=account.name or "")
        proven.append((person, entity.url))

    for person, url in proven:
        key, name = normalise_linkedin(person.linkedin_url), _name_key(person.full_name)
        if (key and key in seen_urls) or (name and name in seen_names):
            step.discarded["duplicate"] += 1
            continue
        if len(step.people) < want:
            step.people.append(person)
            step.sources[person.full_name] = url
            seen_urls.add(key)
            seen_names.add(name)

    # Every proven person with a profile, not only those taken: the next workspace pays nothing.
    from nexus.people import pool

    await pool.enqueue_pool(ts.tenant_id, people=[
        pool.linkedin_fact(p, domain, source=pool.SOURCE_PARALLEL)
        for p, _ in proven if p.linkedin_url])
    step.note = "found" if step.people else "nobody_fits"
    step.discarded = +step.discarded
    return step
