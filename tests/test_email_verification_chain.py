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


def test_provider_from_mx_classifies_esp():
    from nexus.verification.provider import provider_from_mx

    assert provider_from_mx(["aspmx.l.google.com", "alt1.aspmx.l.google.com"]) == "gsuite"
    assert provider_from_mx(["acme-com.mail.protection.outlook.com"]) == "office365"
    assert provider_from_mx(["mx1.self-hosted.io"]) == "custom"
    assert provider_from_mx([]) is None
    assert provider_from_mx(None) is None


async def test_dns_verifier_surfaces_provider_from_mx(monkeypatch):
    from nexus.verification import dns as dnsmod

    def fake_resolve(domain, timeout):
        return (STATUS_RISKY, 0.5, {"mx": ["aspmx.l.google.com"]})

    monkeypatch.setattr(dnsmod.DnsMxEmailVerifier, "_resolve", staticmethod(fake_resolve))
    verdict = await dnsmod.DnsMxEmailVerifier().verify_one("a@acme.com")
    assert verdict.provider_type == "gsuite"


async def test_reverify_persists_provider_to_custom_fields():
    from nexus.enrichment.reverify import reverify_contact
    from nexus.models.account import Contact

    contact = Contact(full_name="A", account_id="x", email="a@acme.com", custom_fields={})

    async def verify(email):
        return EmailVerification(
            email=email, status=STATUS_RISKY, confidence=0.5,
            source="reacher", provider_type="office365",
        )

    changed = await reverify_contact(contact, verify)
    assert changed is True
    assert contact.email_status == STATUS_RISKY
    assert (contact.custom_fields or {}).get("email_provider") == "office365"


async def test_pattern_guess_sanitizes_apostrophe_names():
    """The blind pattern guesser must emit an RFC-valid local-part: O'Mara -> omara."""
    from nexus.enrichment.providers import _EMAIL, PatternEmailProvider
    from nexus.models.account import Account, Contact

    acct = Account(name="Stripe", domain="stripe.com")
    contact = Contact(full_name="Eileen O'Mara", account_id="a")
    result = await PatternEmailProvider().enrich(acct, contact)
    assert result.email == "eileen.omara@stripe.com"
    assert _EMAIL.match(result.email)  # would have been invalid before the fix
