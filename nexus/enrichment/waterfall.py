"""Waterfall enrichment: try providers in order until coverage meets the confidence threshold.

Merges partial results (e.g. one provider supplies email, another phone) and persists the best
values onto the contact. Never raises — a failing provider is skipped.
"""
from __future__ import annotations

import logging

from nexus.core.tenancy import TenantSession
from nexus.enrichment.providers import (
    EnrichmentProvider,
    EnrichmentResult,
    PatternEmailProvider,
    SearchEnrichmentProvider,
)
from nexus.models.account import Account, Contact

logger = logging.getLogger("nexus.enrichment.waterfall")


class WaterfallEnricher:
    def __init__(self, providers: list[EnrichmentProvider], min_confidence: float = 0.6):
        if not providers:
            raise ValueError("WaterfallEnricher needs at least one provider")
        self.providers = providers
        self.min_confidence = min_confidence

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

        if merged.email and merged.email_confidence >= contact.email_confidence:
            contact.email, contact.email_confidence = merged.email, merged.email_confidence
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
                PatternEmailProvider(),
            ]
        )
    return _enricher


def set_enricher(enricher: WaterfallEnricher) -> None:
    global _enricher
    _enricher = enricher
