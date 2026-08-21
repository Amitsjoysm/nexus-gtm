"""Waterfall enrichment: try providers in order until coverage meets the confidence threshold.

Merges partial results (e.g. one provider supplies email, another phone) and persists the best
values onto the contact. A failing provider is skipped rather than fatal.

**The provider order is also the billing boundary.** Providers are split by ``costs_money``: free
ones run outside the meter, and only the paid remainder runs inside it. So the tenant is charged
once for one enriched contact however we obtained it, and the usage row records which — the saving
a registered source database brings is COGS, not price.

The one thing this raises is ``QuotaExceeded``, and only when the caller asks for it via
``raise_on_block``. Background callers get an empty result instead, because a quota on enrichment
must never take down the sweep it happens to run inside.
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
    SourceDatabaseProvider,
    VerifyingPatternEmailProvider,
)
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_VALID

logger = logging.getLogger("nexus.enrichment.waterfall")

# What the billing seam charges for one enriched contact. Priced in `billing/rates.py` as
# "search + finder + verify", which is what the paid half of the waterfall spends.
CONTACT_CAPABILITY = "enrich.contact"


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

    def _satisfied(self, merged: EnrichmentResult) -> bool:
        """Both channels clear the bar, so consulting anyone else would spend money for nothing."""
        return merged.email_confidence >= self.min_confidence and (
            merged.phone_confidence >= self.min_confidence or bool(merged.phone)
        )

    async def _consult(
        self, providers: list[EnrichmentProvider], account: Account, contact: Contact,
        merged: EnrichmentResult,
    ) -> None:
        """Run providers in order, merging the best value per field. Stops early once satisfied."""
        for provider in providers:
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
            if self._satisfied(merged):
                break

    async def enrich_contact(
        self, ts: TenantSession, contact: Contact, account: Account | None = None,
        *, user_id: str | None = None, raise_on_block: bool = False,
    ) -> EnrichmentResult:
        """Enrich a contact through the waterfall, billing ``enrich.contact`` for what it spends.

        The providers are split by ``costs_money``, and that split is the billing boundary. Free
        ones — today a registered source database — run **outside** the meter: they spend nothing,
        and an answer already in hand must not be refused. Only if they leave a gap is the paid
        remainder consulted, and that runs **inside** ``metered()`` so a blocked tenant is stopped
        *before* the search request and the verification credit rather than after.

        Either way the customer is charged once for one enriched contact. What a source database
        changes is our COGS, not the price — `cached` on the usage row is what makes that visible.

        ``raise_on_block`` separates a person pressing "Enrich" (who should get a 402 with the
        upsell) from a campaign sourcing sweep (which should skip the contact and keep going).
        """
        account = account or await ts.get(Account, contact.account_id)
        if account is None:
            return EnrichmentResult()

        merged = EnrichmentResult()
        free = [p for p in self.providers if not p.costs_money]
        paid = [p for p in self.providers if p.costs_money]

        await self._consult(free, account, contact, merged)

        if free and self._satisfied(merged):
            # Answered without spending anything. Metered like the paid waterfall it replaced, and
            # deliberately never blocked — the same posture as a shared-record hit in
            # `nexus/people/enrich.py`, where the answer is already ours to give.
            from nexus.sources.provider import meter_hit

            await meter_hit(ts, CONTACT_CAPABILITY, user_id=user_id)
        elif paid:
            from nexus.billing.errors import QuotaExceeded
            from nexus.billing.meter import metered

            try:
                async with metered(
                    ts, CONTACT_CAPABILITY, user_id=user_id, source="enrichment",
                    attrs={"provider": "waterfall", "cached": False},
                ):
                    await self._consult(paid, account, contact, merged)
            except QuotaExceeded:
                if raise_on_block:
                    raise
                logger.info(
                    "contact enrichment skipped for %s: %s quota reached",
                    contact.id, CONTACT_CAPABILITY,
                )
                return merged

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
                # Cheapest first. A registered source database costs nothing at the margin, so it
                # is asked before anything that spends a search call, a verification credit or an
                # actor run. With no source registered it returns immediately and the order below
                # is unchanged — which is why this is safe to put first unconditionally.
                SourceDatabaseProvider(),
                SearchEnrichmentProvider(get_browser_provider()),
                VerifyingPatternEmailProvider(),
                PatternEmailProvider(),
            ]
        )
    return _enricher


def set_enricher(enricher: WaterfallEnricher) -> None:
    global _enricher
    _enricher = enricher
