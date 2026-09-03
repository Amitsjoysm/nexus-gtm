"""DNS/MX email verification — a free, no-infra deliverability check.

Confirms an address's *domain* can receive mail (MX, or A/AAAA implicit MX per RFC 5321). This
catches dead, reserved, or mistyped domains without any external API or SMTP probing, so it is a
strict upgrade over the syntax-only stub. It deliberately does NOT probe the individual mailbox —
that requires authenticated SMTP, which must run off-host (see :class:`ReacherEmailVerifier`).

Grading:
  * malformed syntax                      -> ``invalid``  (0.9)
  * domain publishes MX                   -> ``risky``    (0.5)  domain accepts mail, mailbox TBD
  * no MX but has A/AAAA (implicit MX)     -> ``risky``    (0.4)
  * nothing resolves / NXDOMAIN           -> ``invalid``  (0.8)
  * any DNS error / timeout               -> ``unknown``  (0.0)  fail-safe: never a false invalid

Never raises across the boundary.
"""
from __future__ import annotations

import asyncio
import re
import time

from nexus.verification.provider import (
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    EmailVerification,
    EmailVerificationProvider,
    provider_from_mx,
)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})$")

# (status, confidence, signals) tuple grade type.
_Grade = tuple[str, float, dict]


class DnsMxEmailVerifier(EmailVerificationProvider):
    """Deliverability by DNS records. Domain verdicts are cached per-process (TTL) so a workspace
    re-verify doesn't re-query the same domain for every contact."""

    #: Reads MX/A records; never contacts a mailbox. So this verifier can say "this domain cannot
    #: receive mail" but never "this address exists", and its `risky` is a floor rather than a
    #: verdict. `CompositeEmailVerifier` relies on this to stop it pre-empting an SMTP probe.
    probes_mailbox = False

    name = "dns"

    def __init__(self, *, timeout_s: float = 5.0, cache_ttl_s: float = 3600.0) -> None:
        self._timeout = timeout_s
        self._ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, _Grade]] = {}

    async def verify_one(self, email: str) -> EmailVerification:
        addr = (email or "").strip()
        m = _EMAIL_RE.match(addr)
        if not m:
            return EmailVerification(
                email=addr, status=STATUS_INVALID, confidence=0.9, source=self.name,
            )
        status, confidence, signals = await self._grade_domain(m.group(1).lower())
        # Surface the ESP (gsuite/office365/…) from the MX hosts when we have them.
        provider = provider_from_mx(signals.get("mx"))
        return EmailVerification(
            email=addr, status=status, confidence=confidence,
            source=self.name, provider_type=provider, signals=signals,
        )

    async def _grade_domain(self, domain: str) -> _Grade:
        now = time.monotonic()
        cached = self._cache.get(domain)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        try:
            grade = await asyncio.wait_for(
                asyncio.to_thread(self._resolve, domain, self._timeout), self._timeout + 1.0
            )
        except Exception:
            # DNS hiccup / timeout must never demote a good address to invalid.
            return (STATUS_UNKNOWN, 0.0, {"dns": "error"})
        self._cache[domain] = (now, grade)
        return grade

    @staticmethod
    def _resolve(domain: str, timeout_s: float) -> _Grade:
        import dns.resolver  # lazy: only imported when the 'dns' verifier is selected

        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout_s
        resolver.lifetime = timeout_s
        empty = (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers)

        try:
            mx = resolver.resolve(domain, "MX")
            hosts = sorted(str(r.exchange).rstrip(".") for r in mx if str(r.exchange).strip("."))
            if hosts:
                return (STATUS_RISKY, 0.5, {"mx": hosts[:3]})
        except empty:
            pass

        # No usable MX: RFC 5321 falls back to the domain's A/AAAA as an implicit MX.
        for rtype in ("A", "AAAA"):
            try:
                if list(resolver.resolve(domain, rtype)):
                    return (STATUS_RISKY, 0.4, {"implicit_mx": rtype})
            except empty:
                continue

        return (STATUS_INVALID, 0.8, {"dns": "no_mx_or_a"})
