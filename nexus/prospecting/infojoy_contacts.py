"""Find contacts, second source: InfoJoy's contact database (owner decision, 2026-10-26).

The chain is the shared pool, then InfoJoy, then a LinkedIn read, then web search: "pool, then
InfoJoy, but add others as well for fallback". InfoJoy is asked for people at the account's own
website with the ICP's titles, as LinkedIn is, and through the same shared page cache Prospector
uses (`prospector.fetch.fetch_page`), so one domain and set of titles is bought once a day for every
workspace.

Every row must prove it is about this account and fits, the rule every source here follows:

* **its company website is the account's domain** (``CompanyUrl``, normalised). The provider's
  filter is a candidate filter, not proof, as `sources/provider.py` says of a foreign database;
* **its title fits the ICP** (`lookalike.contacts.title_fits`, strict when titles are mandatory);
* **it is nobody already held** (profile, name or email).

InfoJoy's email is the provider's, unverified (Prospector's D19). Here it is checked with the live
verifier before it is kept: deliverable is kept and charged as one `enrich.contact`, like every
other Find contacts email; anything else goes to the waterfall's own finder, which charges for
itself. Never raises: a provider that is not set up or not answering is a note, and the chain moves
on.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.prospecting.infojoy_contacts")

#: Rows asked for: enough for the title gate to leave a few, bounded because InfoJoy bills per row.
#: One size for Find contacts and the discovery buyer check, so they share one cached page.
PAGE_ROWS = 15


@dataclass
class InfoJoyPeople:
    people: list = field(default_factory=list)          # VendorContact
    #: "" (asked), "not_configured", "unavailable", "no_domain", "no_titles", "nobody_fits"
    note: str = ""
    detail: str = ""
    discarded: Counter = field(default_factory=Counter)
    vendor_rows: int = 0


async def infojoy_people(ts, account, icp: dict, *, want: int, seen_urls: set[str],
                         seen_names: set[str], seen_emails: set[str]) -> InfoJoyPeople:
    """Up to ``want`` InfoJoy people at ``account`` who fit the ICP. Updates the ``seen_*`` sets."""
    from nexus.companies.resolution import normalise_domain
    from nexus.integrations.company_search import domain_from_url
    from nexus.lookalike.contacts import icp_title_targets, title_fits
    from nexus.people.store import normalise_linkedin
    from nexus.prospecting.contacts import _name_key, linkedin_search_titles
    from nexus.prospector.errors import ProspectorError
    from nexus.prospector.fetch import fetch_page, record_browse_cost
    from nexus.prospector.filters import MAX_VALUES, SearchFilters
    from nexus.prospector.records import parse_contact
    from nexus.prospector.search import client

    out = InfoJoyPeople()
    domain = normalise_domain(account.domain)
    if not domain:
        out.note = "no_domain"
        return out
    titles = linkedin_search_titles(icp or {})
    if not titles:
        out.note = "no_titles"
        return out
    try:
        payload = SearchFilters(company_domains=[domain],
                                titles=titles[:MAX_VALUES]).api_payload("contacts")
        fetched = await fetch_page("contacts", payload, page=1, size=PAGE_ROWS, client=client(),
                                   ts=ts)
    except ProspectorError as exc:
        out.note = "not_configured" if exc.status == 400 else "unavailable"
        out.detail = exc.detail
        return out
    except Exception:  # never the reason Find contacts fails
        logger.warning("InfoJoy contacts for %s failed", domain, exc_info=True)
        out.note = "unavailable"
        return out

    out.vendor_rows = fetched.vendor_rows
    targets = icp_title_targets(icp or {})
    for item in fetched.parsed.items:
        record = parse_contact(item)
        if normalise_domain(domain_from_url(record.company_url) or record.company_url) != domain:
            out.discarded["wrong_company"] += 1
            continue
        if not title_fits(record.title or "", targets):
            out.discarded["wrong_title"] += 1
            continue
        url = normalise_linkedin(record.linkedin_url)
        name = _name_key(record.full_name)
        email = (record.email or "").strip().lower()
        if (url and url in seen_urls) or (name and name in seen_names) or (
                email and email in seen_emails):
            out.discarded["duplicate"] += 1
            continue
        if len(out.people) >= want:
            continue
        seen_urls.add(url)
        seen_names.add(name)
        seen_emails.add(email)
        out.people.append(record)
    if not out.people:
        out.note = "nobody_fits"
    # COGS, never charged (browsing's own capability). After the cache write in `fetch_page`.
    await record_browse_cost(ts, rows=out.vendor_rows, user_id=None, view="find_contacts")
    return out


#: Companies checked for a buyer at once: InfoJoy answers in 0.5-5 s warm.
BUYER_CHECK_CONCURRENCY = 4


async def buyers_at(companies: list[tuple[str, str]], icp: dict) -> tuple[dict[str, bool], int]:
    """``({domain: someone in the ICP's titles works there}, InfoJoy rows bought)``.

    Contact-first discovery (D2, 2026-10-26): a company is offered only once a buyer is found. The
    shared pool is asked first (free), then InfoJoy, through the shared page cache, so a Find
    contacts on the company the same day re-reads the page without buying it again. Network and
    platform reads only: safe beside an open tenant transaction, and run a few at a time. An
    InfoJoy that cannot answer counts as no buyer found, so the company is offered again later
    rather than added unproven."""
    import asyncio

    from nexus.companies.resolution import normalise_domain
    from nexus.integrations.company_search import domain_from_url
    from nexus.lookalike.contacts import icp_title_targets, title_fits
    from nexus.people.pool import pooled_people_at
    from nexus.prospecting.contacts import linkedin_search_titles
    from nexus.prospector.fetch import fetch_page
    from nexus.prospector.filters import MAX_VALUES, SearchFilters
    from nexus.prospector.records import parse_contact
    from nexus.prospector.search import client

    targets = icp_title_targets(icp or {})
    titles = linkedin_search_titles(icp or {})[:MAX_VALUES]
    gate = asyncio.Semaphore(BUYER_CHECK_CONCURRENCY)
    bought = 0

    async def one(domain: str, name: str) -> tuple[str, bool]:
        nonlocal bought
        domain = normalise_domain(domain)
        if not domain or not titles:
            return domain, False
        async with gate:
            try:
                if await pooled_people_at(domain, targets, limit=1, company_name=name or domain):
                    return domain, True
                payload = SearchFilters(company_domains=[domain],
                                        titles=titles).api_payload("contacts")
                fetched = await fetch_page("contacts", payload, page=1, size=PAGE_ROWS,
                                           client=client())
            except Exception:
                return domain, False
        bought += fetched.vendor_rows
        for item in fetched.parsed.items:
            record = parse_contact(item)
            same = normalise_domain(domain_from_url(record.company_url) or record.company_url)
            if same == domain and title_fits(record.title or "", targets):
                return domain, True
        return domain, False

    results = await asyncio.gather(*(one(d, n) for d, n in companies))
    return dict(results), bought


async def persist_infojoy_people(ts, account, people) -> list:
    """InfoJoy people as contacts, each email checked before it is kept."""
    from nexus.enrichment.linkedin import person_profile_url
    from nexus.integrations.provenance import record_present
    from nexus.models.account import Contact

    created = []
    for person in people:
        profile = person_profile_url(person.linkedin_url)
        contact = Contact(
            tenant_id=ts.tenant_id, account_id=account.id, full_name=person.full_name[:200],
            title=person.title or None, seniority=(person.seniority[:40] or None),
            linkedin_url=(profile[:255] if profile else None),
            enrichment_source="sourcing:infojoy",
        )
        record_present(contact, "infojoy", detail="InfoJoy contact record")
        ts.add(contact)
        await ts.flush()
        await _email_for(ts, contact, account, (person.email or "").strip())
        created.append(contact)
    return created


async def _email_for(ts, contact, account, offered: str) -> None:
    """Keep InfoJoy's address if the live verifier says it may be kept, charged as one contact
    enrichment; otherwise ask the waterfall, which finds and charges for itself."""
    from nexus.billing.errors import QuotaExceeded
    from nexus.billing.meter import metered
    from nexus.enrichment.policy import check_level, keep_address, remember_rejected
    from nexus.enrichment.reverify import apply_verdict, fresh_verify
    from nexus.enrichment.waterfall import CONTACT_CAPABILITY, _verdict_detail, get_enricher
    from nexus.integrations.provenance import record
    from nexus.verification import STATUS_INVALID

    verdict = None
    if offered:
        try:
            verdict = await fresh_verify()(offered)
        except Exception:
            logger.info("verifying an InfoJoy address failed", exc_info=True)
    if verdict is not None and keep_address(verdict.status, check_level(verdict.source)):
        try:
            async with metered(ts, CONTACT_CAPABILITY, source="enrichment",
                               attrs={"provider": "infojoy", "cached": False}):
                contact.email = offered
                apply_verdict(contact, verdict)
                record(contact, "email", "infojoy", detail="from InfoJoy; " + _verdict_detail(
                    contact.email_status, check_level(verdict.source)))
        except QuotaExceeded:
            contact.email = None        # nothing charged, nothing kept
        await ts.flush()
        return
    if verdict is not None and verdict.status == STATUS_INVALID:
        remember_rejected(contact, [offered])
    try:
        await get_enricher().enrich_contact(ts, contact, account)
    except Exception:  # enrichment is best-effort, as for every other sourced contact
        pass
