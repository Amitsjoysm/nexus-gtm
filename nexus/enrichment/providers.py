"""Enrichment providers: derive email/phone for a contact. Tried in order by the waterfall."""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from nexus.enrichment.browser import BrowserProvider
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_INVALID, STATUS_VALID, EmailVerification

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE = re.compile(r"(?:\+?\d[\d\-\s().]{7,}\d)")


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
        first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
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
        phones = _PHONE.findall(blob)
        if phones:
            result.phone = re.sub(r"[\s().\-]", "", phones[0])
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
            locals_ = [f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}",
                       first, f"{first[0]}.{last}"]
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
            return EnrichmentResult()  # everything invalid
        _, _, verdict = best
        # Return the canonical guess (deterministic) at the best non-invalid verdict.
        return EnrichmentResult(
            found=True, email=canonical, email_confidence=verdict.confidence,
            email_status=verdict.status, provider_type=verdict.provider_type,
            source=self.name,
        )
