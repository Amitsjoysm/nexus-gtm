"""Enrichment providers: derive email/phone for a contact. Tried in order by the waterfall."""
from __future__ import annotations

import abc
import re
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable

from nexus.enrichment.browser import BrowserProvider
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_INVALID, STATUS_VALID, EmailVerification

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Deliberately loose: it runs over search-result prose, so it must catch a number in any
# formatting. Loose means it also matches things that are NOT numbers — measured on live contacts,
# it swallowed "Education 2009 - 2013" and stored `20092013`. The regex is the CANDIDATE finder;
# `looks_like_phone` is what decides, and every hit must pass it (see `_first_usable_phone`).
_PHONE = re.compile(r"(?:\+?\d[\d\-\s().]{7,}\d)")


def _first_usable_phone(blob: str) -> str:
    """The first regex hit in ``blob`` that is actually a phone number, canonicalised.

    Two things this path was missing entirely. It took `phones[0]` — the FIRST match, whether or
    not it was a number — and it validated nothing, so a year range scraped from an education
    snippet became a contact's phone. Scanning past the junk matters as much as rejecting it: a
    profile that mentions "2009 - 2013" before the real number would otherwise yield the date.

    Canonicalises through the same `normalise_phone` the rest of the product uses, so this path
    stops being the one that writes bare digit soup into the column.
    """
    from nexus.contacts.phone import looks_like_phone, normalise_phone

    for hit in _PHONE.findall(blob):
        candidate = re.sub(r"[\s().\-]", "", hit)
        if not looks_like_phone(candidate):
            continue
        normalised = normalise_phone(candidate)
        return normalised.e164 or candidate
    return ""


def _local_part(name: str) -> str:
    """One name token -> a safe email local-part: fold accents to ASCII, drop everything but
    ``[a-z0-9]``. "O'Mara" -> "omara", "Renée" -> "renee". Empty if nothing survives."""
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", folded.lower())


@dataclass(slots=True)
class EnrichmentResult:
    found: bool = False
    email: str | None = None
    email_confidence: float = 0.0
    phone: str | None = None
    phone_confidence: float = 0.0
    source: str | None = None
    # Deliverability verdict that travels back with a found email (verifying finder).
    email_status: str | None = None
    provider_type: str | None = None

    @property
    def best_confidence(self) -> float:
        return max(self.email_confidence, self.phone_confidence)


class EnrichmentProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult: ...


class PatternEmailProvider(EnrichmentProvider):
    """Zero-dependency baseline: guess a corporate email from name + domain. Low confidence."""

    name = "pattern"

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        if not account.domain or not contact.full_name.strip():
            return EnrichmentResult()
        parts = re.split(r"\s+", contact.full_name.strip().lower())
        # Strip apostrophes/diacritics/punctuation from each name part so the guessed address is
        # RFC-valid: "Eileen O'Mara" -> eileen.omara@…, not eileen.o'mara@… (which parses invalid).
        first = _local_part(parts[0])
        last = _local_part(parts[-1]) if len(parts) > 1 else ""
        if not first and not last:
            return EnrichmentResult()
        domain = account.domain.lower().lstrip("@")
        guess = f"{first}.{last}@{domain}" if last else f"{first}@{domain}"
        return EnrichmentResult(
            found=True, email=guess, email_confidence=0.4, source=self.name
        )


class SearchEnrichmentProvider(EnrichmentProvider):
    """Find email/phone from public web results via the browser provider. Higher confidence."""

    name = "search"

    def __init__(self, browser: BrowserProvider):
        self.browser = browser

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        domain = (account.domain or "").lower().lstrip("@")
        query = f'"{contact.full_name}" {account.name} email contact'
        hits = await self.browser.search(query, limit=6)
        blob = " ".join((h.get("snippet", "") + " " + h.get("title", "")) for h in hits)

        result = EnrichmentResult(source=self.name)
        for em in _EMAIL.findall(blob):
            # Prefer an address on the company domain.
            if domain and em.lower().endswith("@" + domain):
                result.email, result.email_confidence, result.found = em, 0.8, True
                break
            if not result.email:
                result.email, result.email_confidence, result.found = em, 0.55, True
        phone = _first_usable_phone(blob)
        if phone:
            result.phone = phone
            result.phone_confidence = 0.5
            result.found = True
        return result


class VerifyingPatternEmailProvider(EnrichmentProvider):
    """Permutation-based email finder that *scores* each guess via a real verifier.

    Builds a bounded set of corporate-email permutations from the contact's name + the
    account domain, verifies them in priority order, and stops on the first ``valid``. It is
    catch-all aware (a catch-all domain makes every guess look deliverable, so it returns the
    canonical guess flagged risky rather than blasting probes). With no real verifier wired,
    every probe comes back ``unknown`` and the blind ``PatternEmailProvider`` after it in the
    waterfall still supplies the 0.4 guess — so the offline path is unchanged.
    """

    name = "pattern_verified"

    def __init__(
        self,
        *,
        verify: Callable[[str], Awaitable[EmailVerification]] | None = None,
        max_candidates: int | None = None,
    ):
        self._verify = verify
        self._max = max_candidates

    async def _resolve_verify(self) -> Callable[[str], Awaitable[EmailVerification]]:
        if self._verify is not None:
            return self._verify
        # Lazy default: the registry's cached, policy-wrapped verifier.
        from nexus.integrations.registry import get_registry

        return get_registry().verify_email

    def _cap(self) -> int:
        if self._max is not None:
            return self._max
        from nexus.core.config import get_settings

        return get_settings().email_finder_max_candidates

    def _candidates(self, full_name: str, domain: str) -> list[str]:
        parts = re.split(r"\s+", (full_name or "").strip().lower())
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        domain = (domain or "").lower().lstrip("@")
        if not first or not domain:
            return []
        if last:
            fi, li = first[0], last[0]
            # The 10 most common corporate email patterns, in priority order. first.last and
            # first lead (the user's most-wanted), then the next 8 by real-world frequency.
            locals_ = [
                f"{first}.{last}",   # jane.doe   (most common)
                first,               # jane
                f"{first}{last}",    # janedoe
                f"{fi}{last}",       # jdoe
                f"{first}{li}",      # janed
                f"{fi}.{last}",      # j.doe
                f"{first}_{last}",   # jane_doe
                last,                # doe
                f"{last}.{first}",   # doe.jane
                f"{fi}{li}",         # jd
            ]
        else:
            locals_ = [first]
        seen: list[str] = []
        for loc in locals_:
            email = f"{loc}@{domain}"
            if email not in seen:
                seen.append(email)
        return seen[: self._cap()]

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        cands = self._candidates(contact.full_name, account.domain or "")
        if not cands:
            return EnrichmentResult()
        verify = await self._resolve_verify()
        canonical = cands[0]

        best: tuple[float, str, EmailVerification] | None = None  # (rank, email, verdict)
        for i, email in enumerate(cands):
            verdict = await verify(email)
            if i == 0 and verdict.signals.get("is_catch_all"):
                # Catch-all: every guess "works"; return canonical guess flagged risky.
                return EnrichmentResult(
                    found=True, email=canonical, email_confidence=0.5,
                    email_status="risky", provider_type=verdict.provider_type,
                    source=self.name,
                )
            if verdict.status == STATUS_VALID:
                return EnrichmentResult(
                    found=True, email=email, email_confidence=verdict.confidence,
                    email_status=STATUS_VALID, provider_type=verdict.provider_type,
                    source=self.name,
                )
            if verdict.status != STATUS_INVALID:
                rank = 2.0 if verdict.status == "risky" else 1.0
                if best is None or rank > best[0]:
                    best = (rank, email, verdict)

        if best is None:
            return EnrichmentResult()  # every candidate verified invalid
        _, best_email, verdict = best
        # Return the candidate that earned the best non-invalid verdict, paired with THAT verdict.
        # Ties keep the earliest (highest-frequency) pattern — first.last — via the strict `>`
        # above, so the common degraded case still yields the canonical guess. Crucially we never
        # return an address that verified `invalid` (those are excluded from `best`), and we never
        # pair an email with a different candidate's verdict.
        return EnrichmentResult(
            found=True, email=best_email, email_confidence=verdict.confidence,
            email_status=verdict.status, provider_type=verdict.provider_type,
            source=self.name,
        )
