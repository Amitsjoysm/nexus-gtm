"""Fallback-chain (composite) email verifier + DNS/MX verifier + guess sanitization.

All offline: the DNS resolver is monkeypatched so nothing touches the network.
"""
from __future__ import annotations

from nexus.verification.provider import (
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    STATUS_VALID,
    CompositeEmailVerifier,
    EmailVerification,
    EmailVerificationProvider,
    StubEmailVerificationProvider,
    build_email_verifier,
)


class _Fixed(EmailVerificationProvider):
    """Always returns a preset verdict — for exercising the chain deterministically."""

    def __init__(self, name: str, status: str, confidence: float = 0.5) -> None:
        self.name = name
        self._status = status
        self._confidence = confidence

    async def verify_one(self, email: str) -> EmailVerification:
        return EmailVerification(
            email=email, status=self._status, confidence=self._confidence, source=self.name
        )


async def test_chain_falls_through_unknown_to_next_decisive():
    chain = CompositeEmailVerifier([
        _Fixed("reacher", STATUS_UNKNOWN),   # inconclusive -> fall through
        _Fixed("dns", STATUS_RISKY, 0.5),    # decisive -> wins
    ])
    v = await chain.verify_one("a@b.com")
    assert v.status == STATUS_RISKY
    assert v.source == "dns"


async def test_chain_primary_wins_when_decisive():
    chain = CompositeEmailVerifier([
        _Fixed("reacher", STATUS_VALID, 0.95),
        _Fixed("dns", STATUS_RISKY, 0.5),
    ])
    v = await chain.verify_one("a@b.com")
    assert v.status == STATUS_VALID
    assert v.source == "reacher"


async def test_chain_all_unknown_returns_unknown():
    chain = CompositeEmailVerifier([
        _Fixed("reacher", STATUS_UNKNOWN),
        _Fixed("dns", STATUS_UNKNOWN),
    ])
    v = await chain.verify_one("a@b.com")
    assert v.status == STATUS_UNKNOWN


def test_build_verifier_single_vs_chain():
    assert build_email_verifier("stub").name == "stub"
    assert build_email_verifier("dns").name == "dns"
    chain = build_email_verifier("reacher,dns")
    assert isinstance(chain, CompositeEmailVerifier)
    assert chain.name == "reacher+dns"
    assert isinstance(build_email_verifier(""), StubEmailVerificationProvider)


async def test_dns_verifier_malformed_is_invalid():
    from nexus.verification.dns import DnsMxEmailVerifier

    v = await DnsMxEmailVerifier().verify_one("not-an-email")
    assert v.status == STATUS_INVALID


async def test_dns_verifier_grades_from_resolver(monkeypatch):
    from nexus.verification import dns as dnsmod

    def fake_resolve(domain: str, timeout: float):
        if domain == "live.com":
            return (STATUS_RISKY, 0.5, {"mx": ["mx.live.com"]})
        return (STATUS_INVALID, 0.8, {"dns": "no_mx_or_a"})

    monkeypatch.setattr(dnsmod.DnsMxEmailVerifier, "_resolve", staticmethod(fake_resolve))
    verifier = dnsmod.DnsMxEmailVerifier()
    assert (await verifier.verify_one("a@live.com")).status == STATUS_RISKY
    assert (await verifier.verify_one("a@dead.example")).status == STATUS_INVALID


async def test_pattern_guess_sanitizes_apostrophe_names():
    """The blind pattern guesser must emit an RFC-valid local-part: O'Mara -> omara."""
    from nexus.enrichment.providers import _EMAIL, PatternEmailProvider
    from nexus.models.account import Account, Contact

    acct = Account(name="Stripe", domain="stripe.com")
    contact = Contact(full_name="Eileen O'Mara", account_id="a")
    result = await PatternEmailProvider().enrich(acct, contact)
    assert result.email == "eileen.omara@stripe.com"
    assert _EMAIL.match(result.email)  # would have been invalid before the fix
