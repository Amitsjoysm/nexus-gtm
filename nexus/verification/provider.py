# nexus/verification/provider.py
"""Email-verification capability: decide whether an address is deliverable.

An :class:`EmailVerificationProvider` grades one address (:meth:`verify_one`) or many
(:meth:`verify_bulk`). The real adapter ("everifier") MUST run on a separate host/IP so bulk
SMTP probing never spams from the app's own domain/IP. The stub is syntax-only so the system
runs offline. Adapters never raise across the boundary.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# Verification verdicts (kept as plain strings for JSON/blackboard friendliness).
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"
STATUS_RISKY = "risky"


@dataclass(slots=True)
class EmailVerification:
    email: str
    status: str = STATUS_UNKNOWN
    confidence: float = 0.0
    source: str = ""
    # Deliverability adapters (e.g. Reacher) enrich these; stub leaves them at defaults so
    # nothing downstream that ignores them breaks.
    provider_type: str | None = None
    signals: dict = field(default_factory=dict)

    @property
    def is_deliverable(self) -> bool:
        return self.status == STATUS_VALID

    def as_dict(self) -> dict:
        return {"email": self.email, "status": self.status,
                "confidence": self.confidence, "source": self.source,
                "provider_type": self.provider_type, "signals": dict(self.signals)}


class EmailVerificationProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def verify_one(self, email: str) -> EmailVerification: ...

    async def verify_bulk(self, emails: list[str]) -> list[EmailVerification]:
        # Default: fan out one-by-one. Real bulk adapters override with a batched probe.
        return [await self.verify_one(e) for e in emails]


class StubEmailVerificationProvider(EmailVerificationProvider):
    """Syntax-only, zero-network default: well-formed address -> low-confidence ``unknown``."""

    name = "stub"

    async def verify_one(self, email: str) -> EmailVerification:
        if _EMAIL_RE.match((email or "").strip()):
            return EmailVerification(email=email, status=STATUS_UNKNOWN,
                                     confidence=0.3, source=self.name)
        return EmailVerification(email=email, status=STATUS_INVALID,
                                 confidence=0.9, source=self.name)


_verifier: EmailVerificationProvider | None = None


def build_email_verifier(name: str) -> EmailVerificationProvider:
    key = (name or "").strip().lower()
    if key in ("stub", "", "none"):
        return StubEmailVerificationProvider()
    if key == "reacher":
        from nexus.core.config import get_settings
        from nexus.verification.reacher import ReacherEmailVerifier

        s = get_settings()
        return ReacherEmailVerifier(
            url=s.email_verify_url, timeout=s.email_verify_timeout_s,
            auth_header=s.email_verify_auth_header or None,
        )
    # Unknown keys still fail safe to the offline stub.
    return StubEmailVerificationProvider()


def get_email_verifier() -> EmailVerificationProvider:
    global _verifier
    if _verifier is None:
        from nexus.core.config import get_settings

        _verifier = build_email_verifier(get_settings().email_verify_provider)
    return _verifier


def set_email_verifier(provider: EmailVerificationProvider | None) -> None:
    global _verifier
    _verifier = provider
