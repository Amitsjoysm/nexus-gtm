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


# ---- a domain-level check must not pre-empt a mailbox probe -------------------------------------

def _dns_stub(status, conf=0.5):
    from nexus.verification.provider import EmailVerification, EmailVerificationProvider

    class _P(EmailVerificationProvider):
        name = "dns"
        probes_mailbox = False

        async def verify_one(self, email):
            return EmailVerification(email=email, status=status, confidence=conf, source="dns")

    return _P()


def _reacher_stub(status, conf=0.95):
    from nexus.verification.provider import EmailVerification, EmailVerificationProvider

    class _P(EmailVerificationProvider):
        name = "reacher"
        probes_mailbox = True

        async def verify_one(self, email):
            return EmailVerification(email=email, status=status, confidence=conf,
                                     source="reacher")

    return _P()


async def test_dns_risky_does_not_short_circuit_reacher():
    """`NEXUS_EMAIL_VERIFY_PROVIDER=dns,reacher` made Reacher dead configuration.

    `DnsMxEmailVerifier` cannot return `valid` — its entire vocabulary is invalid / risky /
    unknown, because it reads MX records and never probes a mailbox. Any domain that has MX
    records, which is essentially every real business domain, grades `risky`. The composite
    returned the first verdict that was not `unknown`, and `risky` qualified, so DNS answered every
    request and Reacher was never called. Every address came back risky, forever, with the real
    verifier configured, reachable and idle.

    A domain-level `risky` is not the same CLAIM as a mailbox-level one: it means "this domain can
    receive mail, I did not check the mailbox". It is a floor, not a verdict, so it is carried as a
    fallback and the probing verifier still runs.
    """
    from nexus.verification.provider import CompositeEmailVerifier

    chain = CompositeEmailVerifier([_dns_stub("risky"), _reacher_stub("valid")])
    got = await chain.verify_one("someone@stripe.com")
    assert got.status == "valid", f"DNS pre-empted the mailbox probe: {got.status} from {got.source}"
    assert got.source == "reacher"


async def test_dns_invalid_is_still_decisive():
    """The other half. "This domain has no MX and no A record" is a complete answer about
    deliverability — nothing can arrive regardless of the mailbox — so it short-circuits, and the
    SMTP probe is not spent on a domain that cannot receive mail."""
    from nexus.verification.provider import CompositeEmailVerifier

    chain = CompositeEmailVerifier([_dns_stub("invalid", 0.8), _reacher_stub("valid")])
    got = await chain.verify_one("someone@no-such-domain-xyz.test")
    assert got.status == "invalid"
    assert got.source == "dns"


async def test_the_dns_fallback_still_answers_when_reacher_cannot():
    """Why the chain exists at all. Measured against the live Reacher instance: ibm.com returns
    `unknown` because the SMTP probe is refused. A domain-level answer beats no answer."""
    from nexus.verification.provider import CompositeEmailVerifier

    chain = CompositeEmailVerifier([_reacher_stub("unknown", 0.2), _dns_stub("risky")])
    got = await chain.verify_one("someone@ibm.com")
    assert got.status == "risky"
    assert got.source == "dns"


def test_the_real_dns_verifier_declares_it_does_not_probe():
    """The flag has to live on the provider, or the composite is back to guessing from names."""
    from nexus.verification.dns import DnsMxEmailVerifier
    from nexus.verification.reacher import ReacherEmailVerifier

    assert DnsMxEmailVerifier.probes_mailbox is False
    assert ReacherEmailVerifier.probes_mailbox is True


# ---- risky is not one thing ---------------------------------------------------------------------

def _map(payload: dict):
    from nexus.verification.reacher import ReacherEmailVerifier

    v = ReacherEmailVerifier(url="http://verifier.invalid/v0/check_email")
    return v._map("someone@example.com", payload)


async def test_a_deliverable_catch_all_is_separated_from_an_unverifiable_address():
    """Measured against the live instance at 158.69.113.104 on 2026-09-02:

        support@github.com   risky  deliverable=True   catch_all=True   role=True
        info@anthropic.com   risky  deliverable=True   catch_all=True   role=True
        hello@gmail.com      risky  deliverable=False  catch_all=False  role=True

    All three were stored as `risky` at confidence 0.40 with no reason attached, which is what
    makes verification look broken: the first two are addresses the receiving server ACCEPTED, and
    the third is not. `safe` did not appear once — catch-all domains and role accounts both force
    `risky`, and B2B prospecting addresses are overwhelmingly one or the other.

    The status stays `risky` in every case. Reacher declined to certify the mailbox and promoting
    it to `valid` would invent a certainty it explicitly withheld — that is how a campaign bounces.
    What changes is that the REASON and the server's own acceptance are carried, so the screen can
    say "accepted, catch-all domain" instead of an unexplained amber label.
    """
    accepted = _map({
        "is_reachable": "risky",
        "smtp": {"is_deliverable": True, "is_catch_all": True},
        "misc": {"is_role_account": True},
    })
    unproven = _map({
        "is_reachable": "risky",
        "smtp": {"is_deliverable": False, "is_catch_all": False},
        "misc": {"is_role_account": True},
    })

    assert accepted.status == "risky" and unproven.status == "risky"
    assert accepted.signals["is_deliverable"] is True
    assert accepted.confidence > unproven.confidence, (
        "an address the server accepted scores no better than one it did not"
    )
    assert accepted.signals.get("risky_reason") == "catch_all"
    assert unproven.signals.get("risky_reason") == "role_account"


async def test_a_disposable_address_is_the_worst_kind_of_risky():
    got = _map({
        "is_reachable": "risky",
        "smtp": {"is_deliverable": True, "is_catch_all": False},
        "misc": {"is_disposable": True},
    })
    assert got.signals.get("risky_reason") == "disposable"
    assert got.confidence <= 0.2, "a throwaway address ranked alongside a real one"


async def test_safe_and_invalid_are_untouched():
    """The grading applies to `risky` only. Reacher's decisive verdicts are already answers."""
    assert _map({"is_reachable": "safe"}).status == "valid"
    assert _map({"is_reachable": "safe"}).confidence >= 0.9
    assert _map({"is_reachable": "invalid"}).status == "invalid"
