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

# ESP classification from MX host names — so the UI can show whether an address is Google
# Workspace, Microsoft 365, etc. Shared by the DNS verifier and (conceptually) Reacher.
_MX_PROVIDER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gsuite", ("google.com", "googlemail", "l.google.com", "aspmx")),
    ("office365", ("mail.protection.outlook.com", "office365")),
    ("outlook", ("outlook.com", "hotmail", "live.com")),
    ("yahoo", ("yahoodns", "yahoo.com")),
    ("zoho", ("zoho.com", "zohomail")),
    ("proton", ("protonmail", "proton.me")),
)


def provider_from_mx(mx_hosts) -> str | None:
    """Classify the email service provider (gsuite/office365/outlook/…) from MX host names.
    Returns 'custom' when hosts exist but match nothing known, None when there are no hosts."""
    blob = " ".join((str(h) or "").lower() for h in (mx_hosts or []))
    if not blob.strip():
        return None
    for ptype, needles in _MX_PROVIDER_RULES:
        if any(n in blob for n in needles):
            return ptype
    return "custom"


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

    #: Whether this adapter actually asks the receiving server about the MAILBOX.
    #:
    #: A domain-level checker (DNS/MX) and a mailbox prober (Reacher) can both say "risky" and mean
    #: entirely different things: the first means "this domain can receive mail, I did not look at
    #: the mailbox", the second means "the server answered and something about this address is
    #: doubtful". The composite below needs to tell them apart, and a name check would be a second
    #: source of truth that drifts the moment a provider is renamed.
    #:
    #: Defaults to True, so a new adapter that forgets to declare it is treated as authoritative
    #: rather than silently demoted to a fallback that never gets to answer.
    probes_mailbox: bool = True

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


class CompositeEmailVerifier(EmailVerificationProvider):
    """Fallback chain over several verifiers.

    Returns the first *decisive* verdict; an inconclusive one falls through to the next provider and
    is kept as a fallback. This lets a real SMTP verifier (Reacher) lead while a free DNS/MX check
    backs it up — so a verifier outage still yields a domain-level verdict instead of a blanket
    "unknown". Never raises (each member is fail-safe on its own).

    **A NON-PROBING PROVIDER'S ``risky`` IS NOT DECISIVE**, and that distinction is the whole reason
    this class needs `probes_mailbox`. `DnsMxEmailVerifier` cannot return `valid` — it reads MX
    records and never contacts a mailbox, so every domain that has MX records, which is essentially
    every real business domain, grades `risky`. Treating that as decisive made
    `NEXUS_EMAIL_VERIFY_PROVIDER=dns,reacher` a configuration in which DNS answered every request
    and Reacher, configured and reachable, was never called once. Every address came back risky
    forever and nothing reported a problem.

    ``invalid`` from a non-probing provider IS still decisive: "this domain has no MX and no A
    record" is a complete statement about deliverability, and spending an SMTP probe on it would
    buy nothing.
    """

    def __init__(self, providers: list[EmailVerificationProvider]) -> None:
        self._providers = [p for p in providers if p is not None]
        self.name = "+".join(p.name for p in self._providers) or "stub"

    @staticmethod
    def _is_decisive(provider: EmailVerificationProvider, verdict: EmailVerification) -> bool:
        if not verdict or not verdict.status or verdict.status == STATUS_UNKNOWN:
            return False
        if getattr(provider, "probes_mailbox", True):
            return True
        # Domain-level: only a hard negative settles the question.
        return verdict.status == STATUS_INVALID

    async def verify_one(self, email: str) -> EmailVerification:
        best: EmailVerification | None = None
        for provider in self._providers:
            verdict = await provider.verify_one(email)
            if self._is_decisive(provider, verdict):
                return verdict
            # Keep the most informative inconclusive answer, so a chain that never reaches a
            # decisive verdict still returns the domain-level finding rather than a bare "unknown".
            if verdict and (best is None or verdict.confidence > best.confidence):
                best = verdict
        return best or EmailVerification(
            email=email, status=STATUS_UNKNOWN, confidence=0.0, source=self.name
        )


_verifier: EmailVerificationProvider | None = None


def _build_single_verifier(key: str) -> EmailVerificationProvider:
    key = (key or "").strip().lower()
    if key in ("stub", "", "none"):
        return StubEmailVerificationProvider()
    if key in ("dns", "mx"):
        # Free, no-infra deliverability via DNS/MX records (domain-level; no mailbox probe).
        from nexus.verification.dns import DnsMxEmailVerifier

        return DnsMxEmailVerifier()
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


def build_email_verifier(name: str) -> EmailVerificationProvider:
    """Build the verifier from a single key ("reacher") or a fallback chain ("reacher,dns").

    A chain tries each in order and returns the first decisive verdict — so the real SMTP verifier
    leads and the free DNS/MX check is the alternative when it's unreachable or inconclusive.
    """
    keys = [k.strip().lower() for k in (name or "").split(",") if k.strip()]
    if not keys:
        return StubEmailVerificationProvider()
    if len(keys) == 1:
        return _build_single_verifier(keys[0])
    return CompositeEmailVerifier([_build_single_verifier(k) for k in keys])


def get_email_verifier() -> EmailVerificationProvider:
    global _verifier
    if _verifier is None:
        from nexus.core.config import get_settings

        _verifier = build_email_verifier(get_settings().email_verify_provider)
    return _verifier


def set_email_verifier(provider: EmailVerificationProvider | None) -> None:
    global _verifier
    _verifier = provider
