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
from nexus.enrichment.mail_domain import cached_mail_domain, resolve_mail_domain
from nexus.enrichment.policy import (
    check_level,
    forget_email,
    keep_address,
    rejected_emails,
    remember_rejected,
    verification_configured,
)
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_CATCH_ALL, STATUS_INVALID, STATUS_RISKY, STATUS_VALID

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

    def _best(self, candidates: list[EnrichmentResult]) -> EnrichmentResult | None:
        """The address to save, among those the policy allows.

        With a real verifier configured, a stronger verdict beats a higher confidence: a valid
        address outranks a catch-all one however each scored. With the offline stub, confidence alone
        decides, which is today's behaviour. Ties keep the earliest candidate.
        """
        rank = {STATUS_VALID: 3, STATUS_CATCH_ALL: 2, STATUS_RISKY: 2}
        strict = verification_configured()
        best: EnrichmentResult | None = None
        for c in candidates:
            if not keep_address(c.email_status, c.email_check):
                continue
            key = (rank.get(c.email_status or "", 1) if strict else 0, c.email_confidence)
            if best is None or key > (
                rank.get(best.email_status or "", 1) if strict else 0, best.email_confidence
            ):
                best = c
        return best

    def _satisfied(self, merged: EnrichmentResult, candidates: list[EnrichmentResult]) -> bool:
        """Both channels clear the bar, so consulting anyone else would spend money for nothing.

        With a real verifier configured, only a VALID address satisfies the email side: an address
        found without a verdict (a scraped one) may turn out unverifiable, and stopping on it would
        skip the finder that could have proven one.
        """
        best = self._best(candidates)
        if best is None:
            return False
        email_ok = (best.email_status == STATUS_VALID if verification_configured()
                    else best.email_confidence >= self.min_confidence)
        return email_ok and (merged.phone_confidence >= self.min_confidence or bool(merged.phone))

    async def _consult(
        self, providers: list[EnrichmentProvider], account: Account, contact: Contact,
        merged: EnrichmentResult, candidates: list[EnrichmentResult],
    ) -> None:
        """Run providers in order. Every found address is kept as a CANDIDATE — which one is saved is
        decided once, after verification, by the policy — and the best phone is merged as found.
        Stops early once satisfied."""
        for provider in providers:
            try:
                r = await provider.enrich(account, contact)
            except Exception as exc:  # provider isolation
                logger.warning("enrichment provider %s failed: %r", provider.name, exc)
                continue
            if r.rejected:
                merged.rejected = merged.rejected + tuple(r.rejected)
            if not r.found:
                continue
            if r.email:
                candidates.append(r)
            if r.phone and r.phone_confidence > merged.phone_confidence:
                merged.phone, merged.phone_confidence = r.phone, r.phone_confidence
                merged.source = merged.source or r.source
            merged.found = merged.found or bool(r.email or merged.phone)
            if self._satisfied(merged, candidates):
                break

    async def _verify_unchecked(self, candidates: list[EnrichmentResult]) -> None:
        """Give every candidate found without a verdict (search, source database, blind guess) one.

        An address already verified by another provider — the blind guess is usually the finder's
        own first.last — reuses that verdict rather than being checked twice.
        """
        known = {c.email.lower(): c for c in candidates if c.email_status}
        verify = None
        for c in candidates:
            if c.email_status:
                continue
            same = known.get(c.email.lower())
            if same is not None:
                c.email_status, c.email_check = same.email_status, same.email_check
                c.provider_type = c.provider_type or same.provider_type
                if same.email_status == STATUS_VALID:
                    c.email_confidence = max(c.email_confidence, same.email_confidence)
                continue
            try:
                verify = verify or await self._resolve_verify()
                verdict = await verify(c.email)
            except Exception as exc:  # never let verification break enrichment
                logger.warning("final email verify failed for %r: %r", c.email, exc)
                continue
            if verdict and verdict.status:
                c.email_status = verdict.status
                c.email_check = check_level(verdict.source)
                c.provider_type = c.provider_type or verdict.provider_type
                if verdict.status == STATUS_VALID and verdict.confidence > c.email_confidence:
                    c.email_confidence = verdict.confidence
                known[c.email.lower()] = c

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

        # Where does this organisation actually receive email? Resolved once per account and cached
        # for 30 days (`nexus/enrichment/mail_domain.py`), because every guess below is built on it.
        #
        # A contact that already has addresses proven dead is the "suspected wrong domain" case: the
        # patterns were right and the place was not, so the ladder is walked again — reading the
        # company's own site — instead of guessing at the same domain a second time.
        try:
            suspect = bool(rejected_emails(contact)) and (
                (cached_mail_domain(account).source if cached_mail_domain(account) else "none")
                in ("website_domain", "none")
            )
            await resolve_mail_domain(ts, account, force=suspect)
        except Exception as exc:  # resolution must never break enrichment
            logger.warning("mail domain resolution failed for %s: %r", account.id, exc)

        merged = EnrichmentResult()
        candidates: list[EnrichmentResult] = []
        free = [p for p in self.providers if not p.costs_money]
        paid = [p for p in self.providers if p.costs_money]

        await self._consult(free, account, contact, merged, candidates)

        if free and self._satisfied(merged, candidates):
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
                    await self._consult(paid, account, contact, merged, candidates)
            except QuotaExceeded:
                if raise_on_block:
                    raise
                logger.info(
                    "contact enrichment skipped for %s: %s quota reached",
                    contact.id, CONTACT_CAPABILITY,
                )
                return merged

        # Final verification: every candidate found without a verdict (search, source database, the
        # blind guess) gets one, so the choice below is made on evidence rather than on who guessed.
        await self._verify_unchecked(candidates)

        # Invalid is never kept (policy): remember every address proven dead for this person so it
        # is never guessed again, and remove the saved one if that is what was just disproved.
        disproved = [c.email for c in candidates if c.email_status == STATUS_INVALID]
        disproved += list(merged.rejected)
        remember_rejected(contact, disproved)
        if contact.email and contact.email.strip().lower() in rejected_emails(contact):
            forget_email(contact)

        choice = self._best(candidates)
        merged.email = choice.email if choice else None
        merged.email_confidence = choice.email_confidence if choice else 0.0
        merged.email_status = choice.email_status if choice else None
        merged.email_check = choice.email_check if choice else ""
        merged.provider_type = choice.provider_type if choice else None
        if choice:
            merged.source = choice.source
        merged.found = bool(merged.email or merged.phone)

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
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "enrichment.contact", actor_user_id=user_id,
                   refs={"account_id": account.id, "contact_id": contact.id},
                   payload={"found": merged.found, "source": merged.source,
                            "email_found": bool(merged.email),
                            "email_status": merged.email_status,
                            "email_confidence": merged.email_confidence,
                            "phone_found": bool(merged.phone)})
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
