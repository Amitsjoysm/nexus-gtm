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
from dataclasses import dataclass

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


async def source_account_contacts(
    ts: TenantSession, account: Account, *, limit: int = 5, linkedin_client=None,
) -> list[Contact]:
    """Source the buying committee for one account: net-new people, deduped against existing
    contacts, persisted, and email-verified. Never raises — returns the contacts created (possibly
    empty). Used by the account "Find contacts" action and the manual Run-pipeline button (not the
    automated sweep, which stays cheap).

    LinkedIn first (``nexus/prospecting/contacts.py``): people at the account's own page with the
    ICP's titles. The contact-search registry (web search) only when that finds nobody.
    """
    from nexus.relevance.engine import get_profile

    profile = await get_profile(ts)
    icp = (getattr(profile, "icp", None) or {}) if profile else {}

    from nexus.prospecting.contacts import linkedin_contacts

    linkedin = await linkedin_contacts(ts, account, icp, limit=limit, client=linkedin_client)
    if linkedin.notes or linkedin.discarded:
        logger.info("LinkedIn contacts for %s: notes=%s discarded=%s", account.id,
                    linkedin.notes, dict(linkedin.discarded))
    if linkedin.people:
        return await _persist_linkedin_people(ts, account, linkedin.people)

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
        return []

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


async def _persist_linkedin_people(ts: TenantSession, account: Account, people) -> list[Contact]:
    """LinkedIn people as contacts, each then given a work email by the verified finder (the
    waterfall enricher, which also charges for it). Already deduped by the caller."""
    from nexus.enrichment.waterfall import get_enricher

    enricher = get_enricher()
    created: list[Contact] = []
    for person in people:
        contact = Contact(
            tenant_id=ts.tenant_id, account_id=account.id, full_name=person.full_name,
            title=person.title or None, linkedin_url=person.linkedin_url,
            enrichment_source="sourcing:linkedin",
        )
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
