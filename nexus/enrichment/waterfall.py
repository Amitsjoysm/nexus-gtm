"""Waterfall enrichment: try providers in order until coverage meets the confidence threshold.

Merges partial results (e.g. one provider supplies email, another phone) and persists the best
values onto the contact. Never raises — a failing provider is skipped.
"""
from __future__ import annotations

import logging

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.enrichment.providers import (
    EnrichmentProvider,
    EnrichmentResult,
    PatternEmailProvider,
    SearchEnrichmentProvider,
    VerifyingPatternEmailProvider,
)
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_VALID

logger = logging.getLogger("nexus.enrichment.waterfall")


class WaterfallEnricher:
    def __init__(
        self,
        providers: list[EnrichmentProvider],
        min_confidence: float = 0.6,
        verify=None,
    ):
        if not providers:
            raise ValueError("WaterfallEnricher needs at least one provider")
        self.providers = providers
        self.min_confidence = min_confidence
        # Optional verifier for the final pass below. None = the registry's cached verifier
        # (resolved lazily). A test seam too.
        self._verify = verify

    async def _resolve_verify(self):
        if self._verify is not None:
            return self._verify
        from nexus.integrations.registry import get_registry

        return get_registry().verify_email

    async def enrich_contact(
        self, ts: TenantSession, contact: Contact, account: Account | None = None
    ) -> EnrichmentResult:
        account = account or await ts.get(Account, contact.account_id)
        if account is None:
            return EnrichmentResult()

        merged = EnrichmentResult()
        for provider in self.providers:
            try:
                r = await provider.enrich(account, contact)
            except Exception as exc:  # provider isolation
                logger.warning("enrichment provider %s failed: %r", provider.name, exc)
                continue
            if not r.found:
                continue
            # Keep the highest-confidence value for each field.
            if r.email and r.email_confidence > merged.email_confidence:
                merged.email, merged.email_confidence = r.email, r.email_confidence
                merged.email_status = r.email_status
                merged.provider_type = r.provider_type
                merged.source = r.source
            if r.phone and r.phone_confidence > merged.phone_confidence:
                merged.phone, merged.phone_confidence = r.phone, r.phone_confidence
                merged.source = merged.source or r.source
            merged.found = merged.found or bool(merged.email or merged.phone)
            # Stop early once both channels clear the bar.
            if merged.email_confidence >= self.min_confidence and (
                merged.phone_confidence >= self.min_confidence or merged.phone
            ):
                break

        # Final verification: ensure the CHOSEN address carries a deliverability verdict. The
        # search/pattern providers find an email but never verify it, so without this the winning
        # address lands with no status -> "unverified" in the UI even when the verifier is up.
        # Only runs when nothing already attached a verdict (the verifying finder won).
        if merged.email and not merged.email_status:
            try:
                verify = await self._resolve_verify()
                verdict = await verify(merged.email)
                if verdict and verdict.status:
                    merged.email_status = verdict.status
                    if (
                        verdict.status == STATUS_VALID
                        and verdict.confidence > merged.email_confidence
                    ):
                        merged.email_confidence = verdict.confidence
            except Exception as exc:  # never let verification break enrichment
                logger.warning("final email verify failed for %r: %r", merged.email, exc)

        if merged.email and merged.email_confidence >= contact.email_confidence:
            contact.email, contact.email_confidence = merged.email, merged.email_confidence
            # Persist the deliverability verdict too — without this the verified status was
            # computed and then thrown away, leaving every enriched contact "unverified".
            if merged.email_status:
                contact.email_status = merged.email_status
                contact.email_checked_at = utcnow()
                # Persist the detected ESP (gsuite/office365/…) for the UI. Reassign the JSON dict
                # so SQLAlchemy tracks the change.
                if merged.provider_type:
                    cf = dict(contact.custom_fields or {})
                    cf["email_provider"] = merged.provider_type
                    contact.custom_fields = cf
        if merged.phone and merged.phone_confidence >= contact.phone_confidence:
            contact.phone, contact.phone_confidence = merged.phone, merged.phone_confidence
        if merged.found:
            contact.enrichment_source = merged.source
        await ts.flush()
        return merged


_enricher: WaterfallEnricher | None = None


def get_enricher() -> WaterfallEnricher:
    global _enricher
    if _enricher is None:
        from nexus.enrichment.browser import get_browser_provider

        _enricher = WaterfallEnricher(
            providers=[
                SearchEnrichmentProvider(get_browser_provider()),
                VerifyingPatternEmailProvider(),
                PatternEmailProvider(),
            ]
        )
    return _enricher


def set_enricher(enricher: WaterfallEnricher) -> None:
    global _enricher
    _enricher = enricher
