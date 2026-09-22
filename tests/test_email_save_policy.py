"""What the email finder may SAVE, once a real verifier is configured.

Customers reported major deliverability failures, and the finder was writing addresses no verifier
stood behind: the blind first.last guess survived even after every pattern verified INVALID, and an
unverifiable guess was stored as `unknown`/`risky`. The policy, decided with the product owner:

* valid — saved;
* catch-all, or risky from a MAILBOX check (Reacher) — saved, labelled; the user decides sending;
* invalid — never kept, and remembered so it is never guessed again;
* unknown, or risky from DNS alone (which only proves the domain takes mail) — not saved.

The stub verifier ("no verifier configured": dev, tests, offline) keeps today's behaviour, pinned by
`tests/test_email_finder.py`; being strict against a verifier that checks nothing proves nothing.
"""
from __future__ import annotations

import pytest

from nexus.enrichment.policy import check_level, keep_address, verification_configured
from nexus.enrichment.providers import (
    EnrichmentProvider,
    EnrichmentResult,
    PatternEmailProvider,
    VerifyingPatternEmailProvider,
)
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.models.account import Account, Contact
from nexus.verification import (
    STATUS_CATCH_ALL,
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from tests.conftest import make_tenant, tenant_session


@pytest.fixture
def strict(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "email_verify_provider", "reacher,dns")


def _verify(verdicts: dict, default=(STATUS_UNKNOWN, 0.2, "reacher")):
    """{email: (status, confidence, source)}; unlisted addresses get `default`."""
    calls: list[str] = []

    async def verify(email: str) -> EmailVerification:
        calls.append(email)
        status, conf, source = verdicts.get(email, default)
        return EmailVerification(email=email, status=status, confidence=conf, source=source)

    verify.calls = calls  # type: ignore[attr-defined]
    return verify


async def _setup(name="Jane Doe", email=None):
    tid = await make_tenant()
    ts_cm = tenant_session(tid)
    ts = await ts_cm.__aenter__()
    acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    contact = Contact(tenant_id=tid, account_id=acc.id, full_name=name, email=email)
    ts.add(contact)
    await ts.flush()
    return ts_cm, ts, acc, contact


# ---- the rule itself ----------------------------------------------------------------------------

def test_the_level_of_a_check_comes_from_who_made_it():
    assert check_level("reacher") == "mailbox"
    assert check_level("reacher+dns") == "mailbox"
    assert check_level("dns") == "domain"
    assert check_level("stub") == ""
    assert check_level("") == ""


def test_strict_policy(strict):
    assert verification_configured()
    assert keep_address(STATUS_VALID, "mailbox")
    assert keep_address(STATUS_CATCH_ALL, "mailbox")
    assert keep_address(STATUS_RISKY, "mailbox")
    assert not keep_address(STATUS_RISKY, "domain"), "DNS risky only proves the domain takes mail"
    assert not keep_address(STATUS_UNKNOWN, "mailbox")
    assert not keep_address(None, "")
    assert not keep_address(STATUS_INVALID, "mailbox")


def test_the_stub_keeps_todays_behaviour_except_for_invalid():
    assert not verification_configured()
    assert keep_address(STATUS_UNKNOWN, "")
    assert keep_address(None, "")
    assert not keep_address(STATUS_INVALID, "")


# ---- through the waterfall ----------------------------------------------------------------------

async def test_a_guess_every_pattern_disproved_is_never_saved(strict):
    cm, ts, acc, contact = await _setup()
    try:
        verify = _verify({}, default=(STATUS_INVALID, 0.95, "reacher"))
        enricher = WaterfallEnricher(
            providers=[VerifyingPatternEmailProvider(verify=verify), PatternEmailProvider()],
            verify=verify,
        )
        res = await enricher.enrich_contact(ts, contact, acc)
    finally:
        await cm.__aexit__(None, None, None)
    assert contact.email is None, "the blind first.last guess survived a proven-invalid verdict"
    assert res.email is None
    assert "jane.doe@acme.com" in (contact.custom_fields or {}).get("rejected_emails", [])


async def test_a_domain_only_risky_guess_is_not_saved(strict):
    cm, ts, acc, contact = await _setup()
    try:
        verify = _verify({}, default=(STATUS_RISKY, 0.5, "dns"))
        enricher = WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)],
                                     verify=verify)
        await enricher.enrich_contact(ts, contact, acc)
    finally:
        await cm.__aexit__(None, None, None)
    assert contact.email is None


@pytest.mark.parametrize("status,conf", [(STATUS_VALID, 0.95), (STATUS_RISKY, 0.4)])
async def test_a_mailbox_checked_address_is_saved(strict, status, conf):
    cm, ts, acc, contact = await _setup()
    try:
        verify = _verify({"jane.doe@acme.com": (status, conf, "reacher")},
                         default=(STATUS_INVALID, 0.95, "reacher"))
        enricher = WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)],
                                     verify=verify)
        await enricher.enrich_contact(ts, contact, acc)
    finally:
        await cm.__aexit__(None, None, None)
    assert contact.email == "jane.doe@acme.com"
    assert contact.email_status == status


async def test_an_unverifiable_scraped_address_does_not_stop_the_finder(strict):
    # A scraped address carries no verdict; if it could not be verified it must not block the
    # verifying finder from producing a proven one.
    class Scraped(EnrichmentProvider):
        name = "search"

        async def enrich(self, account, contact):
            return EnrichmentResult(found=True, email="jdoe@acme.com", email_confidence=0.8,
                                    phone="+14155550100", phone_confidence=0.8, source=self.name)

    cm, ts, acc, contact = await _setup()
    try:
        verify = _verify({"jane.doe@acme.com": (STATUS_VALID, 0.95, "reacher")})
        enricher = WaterfallEnricher(
            providers=[Scraped(), VerifyingPatternEmailProvider(verify=verify)], verify=verify,
        )
        await enricher.enrich_contact(ts, contact, acc)
    finally:
        await cm.__aexit__(None, None, None)
    assert contact.email == "jane.doe@acme.com"
    assert contact.email_status == STATUS_VALID


async def test_a_saved_address_proven_invalid_is_removed(strict):
    cm, ts, acc, contact = await _setup(email="jane.doe@acme.com")
    try:
        verify = _verify({}, default=(STATUS_INVALID, 0.95, "reacher"))
        enricher = WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)],
                                     verify=verify)
        await enricher.enrich_contact(ts, contact, acc)
    finally:
        await cm.__aexit__(None, None, None)
    assert contact.email is None
    assert contact.email_status is None
