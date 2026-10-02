# nexus/people/pool.py
"""People the platform found, kept so the next workspace does not pay to find them again (2026-10-02).

The shared ``people`` store existed for one purpose, the phone lookup, and held nothing else: on the
local deploy it was empty while workspaces had paid LinkedIn for dozens of buyers. A second
workspace asking Find contacts about the same company bought the same people again.

**Only what WE found or verified is pooled** (decided with the product owner):

* people a LinkedIn employees run returned for an account whose page was proven to be the account's
  own (`prospecting/contacts.py`): profile URL, name, title and the company's domain;
* an address our finder found and a **mailbox** verifier proved valid, for a person with a LinkedIn
  profile, on the company's own domain.

A contact a customer uploaded, imported from a CRM or typed in stays in that workspace, and so does
an address they supplied, even after we verified it: it is their data, not something we found.

**Reuse:**

* Find contacts offers pooled people at the account's domain who fit the ICP's titles and were seen
  on LinkedIn within ``REUSE_WITHIN``, before buying an employees run. Seen means a LinkedIn read:
  delivering a pooled person does not refresh them, or a person would never age out.
* The waterfall's ``SharedPeopleProvider`` offers a pooled verified address for the contact's own
  LinkedIn profile, on the account's domain, as a candidate. It carries no verdict, so the live
  verifier checks it again before it can be saved: an address proven last quarter is a lead, not
  proof.

Identity is the LinkedIn profile, never a name (the package docstring says why). **Writes happen in
the worker** (``pool_found_people``), never inside the request: the pool is platform-global and the
request holds a tenant write transaction, the ordering `people/enrich.py` documents. Everything
here is total: the pool is an optimisation, and a failure in it must never cost a contact.
"""
from __future__ import annotations

import logging
from datetime import timedelta

logger = logging.getLogger("nexus.people.pool")

#: Seen on LinkedIn at that company within this long, or not offered: people change jobs.
REUSE_WITHIN = timedelta(days=180)
#: A pooled address older than this is not offered at all, even as a candidate.
EMAIL_REUSE_WITHIN = timedelta(days=90)
#: `Person.source` for someone a LinkedIn employees run returned.
SOURCE_LINKEDIN = "linkedin_employees"
JOB = "pool_found_people"


def linkedin_fact(person, domain: str) -> dict:
    """What a LinkedIn employees row says about someone: business-card facts, nothing sealed."""
    return {"linkedin": person.linkedin_url, "name": person.full_name, "title": person.title or "",
            "domain": domain}


async def enqueue_pool(tenant_id: str, *, people=(), verified=()) -> None:
    """Ask the worker to pool these. ``people`` are `linkedin_fact` dicts; ``verified`` are contact
    ids whose address the finder just proved (read in the worker, so no address rides the queue).

    Never raises: losing a pool write costs a future saving, raising here would cost the contact
    the request is creating."""
    people, verified = [p for p in people if p], [c for c in verified if c]
    if not tenant_id or not (people or verified):
        return
    try:
        from nexus.workers.queue import Job, get_task_queue

        await get_task_queue().enqueue(Job(name=JOB, payload={
            "tenant_id": tenant_id, "people": people, "verified": verified}))
    except Exception:
        logger.warning("could not queue %d people for the shared pool", len(people) + len(verified),
                       exc_info=True)


def _domains_for(account) -> set[str]:
    from nexus.companies.resolution import normalise_domain
    from nexus.enrichment.mail_domain import cached_mail_domain

    domains = {normalise_domain(getattr(account, "domain", None))}
    cached = cached_mail_domain(account)
    if cached is not None and cached.domain:
        domains.add(normalise_domain(cached.domain))
    return domains - {""}


async def pool_found_people(tenant_id: str, *, people=(), verified=()) -> dict:
    """The worker's half: read what needs reading in the workspace, close that session, then write
    the shared rows on the platform session."""
    from nexus.companies.resolution import normalise_domain
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.enrichment.policy import verification_configured
    from nexus.models.account import Account, Contact
    from nexus.people.store import normalise_linkedin, resolve_person_record
    from nexus.workers.tasks import tenant_session

    report = {"people": 0, "emails": 0}
    facts: list[tuple[str, dict]] = []
    for fact in people:
        if (isinstance(fact, dict) and normalise_linkedin(fact.get("linkedin"))
                and normalise_domain(fact.get("domain"))):
            facts.append(("found", {"linkedin": fact["linkedin"], "name": fact.get("name") or "",
                                    "title": fact.get("title") or "",
                                    "domain": normalise_domain(fact["domain"])}))
    if verified and verification_configured():  # a stub "valid" proves nothing
        try:
            async with tenant_session(tenant_id) as ts:
                for contact_id in verified:
                    contact = await ts.get(Contact, contact_id)
                    if contact is None or not normalise_linkedin(contact.linkedin_url):
                        continue
                    account = await ts.get(Account, contact.account_id)
                    domain = normalise_domain(getattr(account, "domain", None))
                    email = (contact.email or "").strip().lower()
                    # On the company's own domain, or it is a previous employer's address.
                    if (not domain or contact.email_status != "valid"
                            or email.rpartition("@")[2] not in _domains_for(account)):
                        continue
                    # The address is ours (found and proven); the name and title may be the
                    # customer's own upload, so only the address and the profile it belongs to
                    # are shared. A row made this way keeps the default source, so Find contacts
                    # never offers it as a person: it can only answer a workspace that already
                    # knows this profile.
                    facts.append(("verified", {
                        "linkedin": contact.linkedin_url, "name": "", "title": "",
                        "domain": domain, "email": email}))
        except Exception:
            logger.warning("could not read contacts to pool for %s", tenant_id, exc_info=True)
    if not facts:
        return report

    now = utcnow()
    try:
        async with get_platform_sessionmaker()() as session:
            for kind, fact in facts:
                person = await resolve_person_record(
                    session, linkedin_url=fact["linkedin"], full_name=fact["name"],
                    title=fact["title"], company_domain=fact["domain"])
                if person is None:
                    continue
                if kind == "found":
                    # A LinkedIn read of their CURRENT role. Unlike a tenant's edit (which only
                    # fills blanks, so one workspace cannot rewrite another's view), this is newer
                    # evidence than whatever the row holds: a job change moves them.
                    person.company_domain = fact["domain"]
                    person.title = fact["title"] or person.title
                    person.source = SOURCE_LINKEDIN
                    person.updated_at = now
                    report["people"] += 1
                elif await _store_verified_email(session, person, fact["email"]):
                    report["emails"] += 1
            await session.commit()
    except Exception:
        logger.warning("could not write %d people to the shared pool", len(facts), exc_info=True)
    return report


async def _store_verified_email(session, person, email: str) -> bool:
    from nexus.core.crypto import seal_text
    from nexus.core.db import utcnow
    from nexus.people.store import _enc_key, _ensure_identity, hash_value

    from sqlalchemy import select

    from nexus.models.person import PersonIdentity

    digest = hash_value(email)
    owner = (await session.scalars(select(PersonIdentity.person_id).where(
        PersonIdentity.kind == "email", PersonIdentity.value_hash == digest))).first()
    if owner is not None and owner != person.id:
        # The address already belongs to somebody else in the pool. Two people on one address is
        # the wrong-attribution failure this store refuses to merge; it needs a human, not a guess.
        logger.warning("pooled address already belongs to another person; not attaching it")
        return False
    if person.email_hash != digest:
        # A newer proven address replaces the stored one: it was proven at the company they are at
        # now. The old address stays an identity of the same person, so it still resolves to them.
        person.email_hash = digest
        person.email_encrypted = seal_text(email, key=_enc_key())
    person.email_status = "valid"
    person.updated_at = utcnow()
    await _ensure_identity(session, person.id, "email", email)
    return True


async def pooled_people_at(domain: str, targets, *, company_name: str, limit: int) -> list:
    """People seen on LinkedIn at this domain recently who fit the ICP's titles, newest first.
    Never raises; an empty list means "ask LinkedIn"."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.lookalike.contacts import _is_excluded, icp_title_fit
    from nexus.models.person import Person
    from nexus.prospecting.linkedin import LinkedInPerson

    if not domain or limit <= 0 or not targets.wanted:
        return []
    try:
        async with get_platform_sessionmaker()() as session:
            rows = (await session.scalars(
                select(Person)
                .where(Person.company_domain == domain)
                .where(Person.source == SOURCE_LINKEDIN)
                .where(Person.linkedin_url.isnot(None))
                .where(Person.updated_at >= utcnow() - REUSE_WITHIN)
                .order_by(Person.updated_at.desc(), Person.id)
                .limit(100))).all()
    except Exception:
        logger.warning("could not read the shared pool for %s", domain, exc_info=True)
        return []
    out = []
    for row in rows:
        title = row.title or ""
        if _is_excluded(title, targets) or icp_title_fit(title, targets)[0] <= 0:
            continue
        first, _, last = (row.full_name or "").strip().partition(" ")
        out.append(LinkedInPerson(linkedin_url=row.linkedin_url, first_name=first,
                                  last_name=last.strip(), title=title,
                                  company_name=company_name))
        if len(out) >= limit:
            break
    return out


async def pooled_email_for(linkedin_url: str, *, domains: set[str]) -> str | None:
    """A recently proven address for this LinkedIn profile on one of these domains, or None."""
    from sqlalchemy import select

    from nexus.core.crypto import unseal_text
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.person import Person
    from nexus.people.store import _enc_key, normalise_linkedin, person_id_for

    url = normalise_linkedin(linkedin_url)
    if not url or not domains:
        return None
    try:
        async with get_platform_sessionmaker()() as session:
            person = (await session.scalars(select(Person).where(
                Person.id == person_id_for(linkedin=url)))).first()
            if (person is None or person.email_status != "valid" or not person.email_encrypted
                    or person.updated_at is None
                    or _aware(person.updated_at) < utcnow() - EMAIL_REUSE_WITHIN):
                return None
            email = unseal_text(person.email_encrypted, key=_enc_key()).strip().lower()
    except Exception:
        logger.warning("could not read a pooled address", exc_info=True)
        return None
    return email if email and email.rpartition("@")[2] in domains else None


def _aware(value):
    if value.tzinfo is None:
        from datetime import timezone

        return value.replace(tzinfo=timezone.utc)
    return value
