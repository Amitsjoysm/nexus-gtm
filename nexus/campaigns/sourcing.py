"""ContactSourcingService: ensure an account has a contact with a (best-effort) email.

Composes the registry (net-new contact search) and the waterfall enricher (verifying email
finder). Owns no orchestration — the campaign draft phase calls it once when a target would be
skipped for ``SKIP_NO_CONTACT``. Never raises across its boundary: a no-candidate / failed
sourcing returns ``SourcingOutcome(None, False, 0.0)`` so the caller can skip cleanly. All
synthetic personas are provenance-marked (``enrichment_source="sourcing:<provider>"``) and,
offline, never clear the send bar — so they cannot leak into real outreach.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.campaigns.sourcing")


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


def set_contact_sourcing_service(svc: ContactSourcingService | None) -> None:
    global _service
    _service = svc
