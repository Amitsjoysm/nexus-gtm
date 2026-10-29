"""ContactSourcingService: ensure an account has a contact with a (best-effort) email.

Composes the registry (net-new contact search) and the waterfall enricher (verifying email
finder). Owns no orchestration: the account's Find contacts action and the account pipeline agent
call it. (It lived in ``nexus/campaigns/`` because the old campaign draft phase was its first
caller; that engine is gone, spec §13.) Never raises across its boundary: a no-candidate / failed
sourcing returns ``SourcingOutcome(None, False, 0.0)`` so the caller can skip cleanly. All
synthetic personas are provenance-marked (``enrichment_source="sourcing:<provider>"``) and,
offline, never clear the send bar — so they cannot leak into real outreach.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.contacts.sourcing")


@dataclass(slots=True)
class SourcingOutcome:
    contact: Contact | None
    sourced: bool            # True if we created a person or filled a missing email
    email_confidence: float


class ContactSourcingService:
    def __init__(self, *, registry=None, enricher=None):
        self._registry = registry
        self._enricher = enricher

    @property
    def registry(self):
        if self._registry is None:
            from nexus.integrations.registry import get_registry

            self._registry = get_registry()
        return self._registry

    @property
    def enricher(self):
        if self._enricher is None:
            from nexus.enrichment.waterfall import get_enricher

            self._enricher = get_enricher()
        return self._enricher

    async def ensure_contact(
        self, ts: TenantSession, account: Account, *, icp: dict
    ) -> SourcingOutcome:
        """Best-effort: return a contact with an email, never raising across the boundary."""
        try:
            return await self._ensure_contact(ts, account, icp)
        except Exception as exc:  # boundary isolation: a failure must never surface
            logger.warning(
                "contact sourcing failed for account %s: %r", account.id, exc
            )
            return SourcingOutcome(None, False, 0.0)

    async def _ensure_contact(
        self, ts: TenantSession, account: Account, icp: dict
    ) -> SourcingOutcome:
        # Explicit query — never touch the lazy ``account.contacts`` relationship under async.
        contacts = await ts.list(Contact, Contact.account_id == account.id)
        existing = self._best_existing(contacts)

        sourced = False
        synthetic_source: str | None = None  # provenance to preserve through enrichment
        contact = existing
        if contact is None:
            cands = await self.registry.contact_search(account, icp)
            if not cands:
                return SourcingOutcome(None, False, 0.0)
            cand = cands[0]
            synthetic_source = f"sourcing:{cand.source}"
            contact = Contact(
                tenant_id=ts.tenant_id,
                account_id=account.id,
                full_name=cand.full_name,
                title=cand.title,
                seniority=cand.seniority,
                email=cand.email,
                enrichment_source=synthetic_source,
            )
            ts.add(contact)
            await ts.flush()
            sourced = True

        if not contact.email:
            await self.enricher.enrich_contact(ts, contact, account)
            sourced = sourced or bool(contact.email)
            # The enricher stamps ``enrichment_source`` with the email-finder's name; for a
            # net-new persona keep the synthetic provenance so it stays marked (and gated) as
            # sourced rather than masquerading as an organically enriched contact.
            if synthetic_source is not None:
                contact.enrichment_source = synthetic_source
                await ts.flush()

        return SourcingOutcome(contact, sourced, contact.email_confidence)

    @staticmethod
    def _best_existing(contacts: list[Contact]) -> Contact | None:
        if not contacts:
            return None
        with_email = [c for c in contacts if c.email]
        if with_email:
            return max(with_email, key=lambda c: c.email_confidence)
        return contacts[0]


_service: ContactSourcingService | None = None


def get_contact_sourcing_service() -> ContactSourcingService:
    global _service
    if _service is None:
        _service = ContactSourcingService()
    return _service


#: What LinkedIn's note means to the person who pressed Find contacts, and what they can do.
_LINKEDIN_REASONS = {
    "no_titles": "Add buyer titles on the Relevance page: without them LinkedIn is not searched.",
    "no_domain": "This account has no website domain, so its LinkedIn page cannot be proven.",
    "no_verified_page": "No LinkedIn page lists {domain} as its website, so we could not tell "
                        "which page is this company's.",
    "unavailable": "LinkedIn search is unavailable right now.",
}


@dataclass
class FindContactsResult:
    """What one Find contacts did, step by step, so the page can say why it found nobody.

    The chain always knew (LinkedIn's note, the rows it discarded, the web fallback's count) and only
    logged it, so the SDR team changed buyer titles when titles were not what stopped it."""

    contacts: list[Contact]
    #: {"status": found | no_titles | no_domain | no_verified_page | nobody_fits | unavailable,
    #:  "found", "pooled", "discarded": {wrong_company, wrong_title, duplicate}}
    linkedin: dict
    #: {"status": not_needed | found | none | unavailable, "found"}
    web: dict
    #: The account's domain, which the sentence names.
    domain: str = ""
    #: {"status": not_needed | found | nobody_fits | not_configured | unavailable | no_titles |
    #:  no_domain, "found", "discarded"}. Asked after the shared pool, before a LinkedIn read.
    infojoy: dict = field(default_factory=lambda: {"status": "not_needed", "found": 0})
    #: Parallel's people search, asked after the LinkedIn read for what it left short (2026-10-29):
    #: {"status": not_needed | found | nobody_fits | not_configured | out_of_credit | unavailable |
    #:  no_titles | no_domain | rejected, "found", "discarded"}.
    parallel: dict = field(default_factory=lambda: {"status": "not_needed", "found": 0})

    @property
    def message(self) -> str:
        return self._sentence(self.domain or "this company")

    @property
    def outcome(self) -> dict:
        verified = sum(1 for c in self.contacts if c.email and c.email_status == "valid")
        unconfirmed = sum(1 for c in self.contacts
                          if c.email and c.email_status in ("catch_all", "risky"))
        return {"linkedin": dict(self.linkedin), "infojoy": dict(self.infojoy),
                "parallel": dict(self.parallel), "web": dict(self.web),
                "emails": {"verified": verified, "unconfirmed": unconfirmed,
                           "none": sum(1 for c in self.contacts if not c.email)}}

    def message_for(self, account: Account) -> str:
        return self._sentence(account.domain or "this company")

    def _sentence(self, domain: str) -> str:
        if self.contacts:
            sources = [label for label, step in (("InfoJoy", self.infojoy),
                                                 ("LinkedIn", self.linkedin),
                                                 ("company research", self.parallel),
                                                 ("web search", self.web))
                       if step.get("status") == "found"]
            where = " and ".join(sources) or "web search"
            n = len(self.contacts)
            emails = self.outcome["emails"]
            people = "person" if n == 1 else "people"
            if emails["verified"] == n:
                return f"Added {n} {people} from {where}, each with a verified email."
            parts = [f"{emails['verified']} with a verified email"] if emails["verified"] else []
            if emails["unconfirmed"]:
                parts.append(f"{emails['unconfirmed']} whose email cannot be confirmed (the "
                             "domain accepts every address)")
            if emails["none"]:
                parts.append(f"{emails['none']} with no email we could verify")
            return f"Added {n} {people} from {where}: " + ", ".join(parts) + "."
        status = self.linkedin.get("status", "")
        if status == "nobody_fits":
            seen = sum(self.linkedin.get("discarded", {}).values())
            other = self.linkedin.get("discarded", {}).get("wrong_company", 0)
            reason = (f"LinkedIn listed {seen} people here, none in one of your buyer titles"
                      + (f" ({other} named another company)" if other else "") + ".")
        else:
            reason = _LINKEDIN_REASONS.get(status, "LinkedIn found nobody.").format(domain=domain)
        # InfoJoy is named only when it was asked: a deployment without it has nothing to say.
        infojoy = self.infojoy.get("status")
        if infojoy == "nobody_fits":
            reason += f" InfoJoy had nobody in your buyer titles at {domain}."
        elif infojoy == "unavailable":
            reason += " InfoJoy is unavailable right now."
        # Company research is named only when it was asked and had something to say; a
        # deployment without a Parallel key says nothing about it.
        parallel = self.parallel.get("status")
        if parallel == "nobody_fits":
            reason += f" Company research had nobody in your buyer titles at {domain}."
        elif parallel in ("unavailable", "out_of_credit", "rejected"):
            reason += " Company research is unavailable right now."
        web = self.web.get("status")
        if web == "unavailable":
            tail = "Web search is unavailable right now too."
        elif web == "none":
            tail = f"Web search found nobody with your buyer titles at {domain} either."
        else:
            tail = ""
        return " ".join(p for p in (reason, tail) if p)


async def source_account_contacts(
    ts: TenantSession, account: Account, *, limit: int = 5, linkedin_client=None,
) -> list[Contact]:
    """The contacts `find_contacts` created, for callers that need only those (the pipeline)."""
    return (await find_contacts(ts, account, limit=limit, linkedin_client=linkedin_client)).contacts


async def find_contacts(
    ts: TenantSession, account: Account, *, limit: int = 5, linkedin_client=None,
    parallel_client=None,
) -> FindContactsResult:
    """Source the buying committee for one account: net-new people, deduped against existing
    contacts, persisted, and email-verified. Never raises for a provider being down; returns what
    was created and why each step found what it found. Used by the account "Find contacts" action
    and the manual Run-pipeline button (not the automated sweep, which stays cheap).

    The chain (owner decision, 2026-10-26): the shared pool, then InfoJoy, then a LinkedIn read of
    the account's own page, each with the ICP's titles and asked only for what the one before left
    short; the contact-search registry (web search) only when all of them found nobody.
    """
    from nexus.relevance.engine import get_profile

    profile = await get_profile(ts)
    icp = (getattr(profile, "icp", None) or {}) if profile else {}

    from nexus.people.store import normalise_linkedin
    from nexus.prospecting.contacts import _name_key, linkedin_contacts
    from nexus.prospecting.infojoy_contacts import infojoy_people, persist_infojoy_people

    pooled = await linkedin_contacts(ts, account, icp, limit=limit, client=linkedin_client,
                                     read=False)
    seen_urls = {normalise_linkedin(p.linkedin_url) for p in pooled.people} - {""}
    seen_names = {_name_key(p.full_name) for p in pooled.people} - {""}
    infojoy = {"status": "not_needed", "found": 0}
    from_infojoy: list = []
    if len(pooled.people) < limit and "no_titles" not in pooled.notes.values():
        held = await ts.list(Contact, Contact.account_id == account.id)
        seen_urls |= {normalise_linkedin(c.linkedin_url) for c in held} - {""}
        seen_names |= {_name_key(c.full_name) for c in held} - {""}
        asked = await infojoy_people(
            ts, account, icp, want=limit - len(pooled.people), seen_urls=seen_urls,
            seen_names=seen_names,
            seen_emails={(c.email or "").strip().lower() for c in held} - {""})
        from_infojoy = asked.people
        infojoy = {"status": "found" if asked.people else asked.note, "found": len(asked.people),
                   "discarded": dict(asked.discarded)}
        if asked.discarded or asked.note:
            logger.info("InfoJoy contacts for %s: note=%s discarded=%s", account.id, asked.note,
                        dict(asked.discarded))

    found = pooled
    if len(pooled.people) + len(from_infojoy) < limit and "no_titles" not in pooled.notes.values():
        read = await linkedin_contacts(
            ts, account, icp, limit=limit - len(pooled.people) - len(from_infojoy),
            client=linkedin_client, pool=False, seen=(seen_urls, seen_names))
        read.people[:0] = pooled.people
        read.pooled |= pooled.pooled
        found = read
    if found.notes or found.discarded:
        logger.info("LinkedIn contacts for %s: notes=%s discarded=%s", account.id,
                    found.notes, dict(found.discarded))
    note = found.notes.get("linkedin", "")
    linkedin = {
        "status": "found" if found.people else (
            "unavailable" if note in ("failed", "not_configured") else note or (
                "not_needed" if from_infojoy else "nobody_fits")),
        "found": len(found.people), "pooled": len(found.pooled),
        "discarded": dict(found.discarded),
    }

    # Parallel's people search for what the LinkedIn read left short (owner decision 2026-10-29).
    parallel = {"status": "not_needed", "found": 0}
    from_parallel: list = []
    parallel_refs: dict = {}
    short = limit - len(found.people) - len(from_infojoy)
    if short > 0 and "no_titles" not in pooled.notes.values():
        from nexus.prospecting.parallel_companies import record_costs
        from nexus.prospecting.parallel_contacts import parallel_people

        seen_urls |= {normalise_linkedin(p.linkedin_url) for p in found.people} - {""}
        seen_names |= {_name_key(p.full_name) for p in found.people} - {""}
        step = await parallel_people(ts, account, icp, want=short, seen_urls=seen_urls,
                                     seen_names=seen_names, client=parallel_client)
        from_parallel, parallel_refs = step.people, step.sources
        parallel = {"status": step.note, "found": len(step.people),
                    "discarded": dict(step.discarded)}
        await record_costs(ts, searches=step.searches, decisions=0)

    if found.people or from_infojoy or from_parallel:
        created = await persist_infojoy_people(ts, account, from_infojoy)
        created += await _persist_linkedin_people(ts, account, found.people, pooled=found.pooled)
        created += await _persist_linkedin_people(ts, account, from_parallel, origin="parallel",
                                                  refs=parallel_refs)
        return FindContactsResult(created, linkedin, {"status": "not_needed", "found": 0},
                                  domain=account.domain or "", infojoy=infojoy,
                                  parallel=parallel)

    created = await _from_web_search(ts, account, icp, limit=limit)
    web = {"status": "found" if created else "none", "found": len(created or [])}
    if created is None:
        web, created = {"status": "unavailable", "found": 0}, []
    return FindContactsResult(created, linkedin, web, domain=account.domain or "",
                              infojoy=infojoy, parallel=parallel)


async def _from_web_search(ts: TenantSession, account: Account, icp: dict, *,
                           limit: int) -> list[Contact] | None:
    """The web fallback. None when the search itself failed, which is not "nobody there"."""
    from nexus.integrations.registry import get_registry

    registry = get_registry()

    from nexus.people.store import normalise_linkedin
    from nexus.prospecting.contacts import _name_key

    existing = await ts.list(Contact, Contact.account_id == account.id)
    seen_emails = {(c.email or "").lower() for c in existing if c.email}
    seen_names = {_name_key(c.full_name) for c in existing if c.full_name}
    # One profile shared four ways (uk.linkedin.com, a trailing slash, tracking parameters) is
    # one person; the email and the name alone let the same human in twice.
    seen_profiles = {normalise_linkedin(c.linkedin_url) for c in existing} - {""}

    from nexus.integrations.search.provider import SearchUnavailable

    try:
        candidates = await registry.contact_search(account, icp, limit=limit)
    except SearchUnavailable:
        # Not a hiccup. "Find contacts" is strictly Exa, and an empty list would tell the rep this
        # company has nobody worth calling when the truth is that search is not configured.
        raise
    except Exception as exc:  # provider hiccup must not break the button
        logger.warning("contact_search failed for %s: %r", account.name, exc)
        return None

    from nexus.enrichment.waterfall import get_enricher

    enricher = get_enricher()
    created: list[Contact] = []
    for cand in candidates:
        if len(created) >= limit:
            break
        # Only persist real people. The "stub" provider returns title-personas
        # ("VP Sales" as the name) for offline/test determinism — never store those as contacts.
        if (cand.source or "").lower() == "stub":
            continue
        email = (cand.email or "").lower()
        name = _name_key(cand.full_name)
        profile_url = normalise_linkedin(cand.linkedin_url)
        duplicate = (
            (email and email in seen_emails)
            or (name and name in seen_names)
            or (profile_url and profile_url in seen_profiles)
        )
        if duplicate:
            continue
        person = Contact(
            tenant_id=ts.tenant_id, account_id=account.id, full_name=cand.full_name,
            title=cand.title, seniority=cand.seniority, email=cand.email,
            # Keep the LinkedIn URL the people-search already extracted (was being dropped),
            # so sourced contacts surface a profile link without a separate enrichment call.
            linkedin_url=cand.linkedin_url,
            enrichment_source=f"sourcing:{cand.source}",
        )
        from nexus.integrations.provenance import record_present

        record_present(person, "web_search", ref=cand.linkedin_url or getattr(cand, "url", None),
                       detail=f"people search ({cand.source})")
        ts.add(person)
        await ts.flush()
        # Guess + verify the work email (the waterfall enricher patterns first.last@domain and
        # verifies it, persisting email + deliverability status onto the contact).
        try:
            await enricher.enrich_contact(ts, person, account)
        except Exception:  # enrichment is best-effort
            pass
        seen_emails.add(email)
        seen_names.add(name)
        if profile_url:
            seen_profiles.add(profile_url)
        created.append(person)
    return created


async def _persist_linkedin_people(ts: TenantSession, account: Account, people,
                                   pooled=frozenset(), *, origin: str = "linkedin",
                                   refs: dict | None = None) -> list[Contact]:
    """Found people as contacts, each then given a work email by the verified finder (the
    waterfall enricher, which also charges for it). Already deduped by the caller. ``pooled`` are
    the profiles the shared pool supplied rather than a read now; ``origin`` names the step that
    found the rest (``linkedin`` or ``parallel``), ``refs`` the page each was found on."""
    from nexus.enrichment.waterfall import get_enricher
    from nexus.integrations.provenance import record_present
    from nexus.people.store import normalise_linkedin

    enricher = get_enricher()
    created: list[Contact] = []
    for person in people:
        from_pool = bool(person.linkedin_url) and normalise_linkedin(person.linkedin_url) in pooled
        contact = Contact(
            tenant_id=ts.tenant_id, account_id=account.id, full_name=person.full_name,
            title=person.title or None, linkedin_url=person.linkedin_url or None,
            enrichment_source="sourcing:pool" if from_pool else f"sourcing:{origin}",
        )
        # The page is the evidence: it names the person, the title and the company.
        ref = (refs or {}).get(person.full_name) or person.linkedin_url or None
        record_present(contact, "pool" if from_pool else origin, ref=ref,
                       detail="found earlier by a LinkedIn read" if from_pool else None)
        ts.add(contact)
        await ts.flush()
        try:
            await enricher.enrich_contact(ts, contact, account)
        except Exception:  # enrichment is best-effort, as for every other sourced contact
            pass
        created.append(contact)
    return created


def set_contact_sourcing_service(svc: ContactSourcingService | None) -> None:
    global _service
    _service = svc
