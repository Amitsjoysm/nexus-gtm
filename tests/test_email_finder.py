"""Verifying email finder: permutation, early-stop on valid, catch-all, degrade. Offline."""
from __future__ import annotations

from nexus.enrichment.providers import (
    PatternEmailProvider,
    VerifyingPatternEmailProvider,
)
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.models.account import Account, Contact
from nexus.verification import (
    STATUS_INVALID,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from tests.conftest import make_tenant, tenant_session


def _fake_verify(verdicts: dict):
    """Return an async verify(email) -> EmailVerification driven by a {email: (status,conf,signals)} map.
    Unlisted emails resolve to unknown/0.2."""

    async def verify(email: str) -> EmailVerification:
        status, conf, signals = verdicts.get(
            email, (STATUS_UNKNOWN, 0.2, {})
        )
        return EmailVerification(
            email=email, status=status, confidence=conf, source="fake", signals=signals
        )

    return verify


async def test_finder_generates_expected_permutations():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}))
    cands = prov._candidates("Jane Doe", "acme.com")
    # 10 most-common corporate patterns, first.last + first leading.
    assert cands == [
        "jane.doe@acme.com",
        "jane@acme.com",
        "janedoe@acme.com",
        "jdoe@acme.com",
        "janed@acme.com",
        "j.doe@acme.com",
        "jane_doe@acme.com",
        "doe@acme.com",
        "doe.jane@acme.com",
        "jd@acme.com",
    ]


async def test_finder_caps_candidates():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}), max_candidates=2)
    cands = prov._candidates("Jane Doe", "acme.com")
    assert len(cands) == 2


async def test_finder_stops_on_first_valid():
    calls = []

    async def verify(email):
        calls.append(email)
        if email == "janedoe@acme.com":
            return EmailVerification(email=email, status=STATUS_VALID, confidence=0.95)
        return EmailVerification(email=email, status=STATUS_INVALID, confidence=0.95)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(verify=verify).enrich(acc, contact)
    assert res.email == "janedoe@acme.com"
    assert res.email_confidence == 0.95
    assert res.email_status == STATUS_VALID
    # Stopped on the first valid (janedoe, the 3rd pattern); never probed the rest.
    assert calls == ["jane.doe@acme.com", "jane@acme.com", "janedoe@acme.com"]


async def test_finder_catch_all_short_circuits_to_canonical_risky():
    calls = []

    async def verify(email):
        calls.append(email)
        return EmailVerification(
            email=email, status="risky", confidence=0.4,
            signals={"is_catch_all": True},
        )

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(verify=verify).enrich(acc, contact)
    assert res.email == "jane.doe@acme.com"  # canonical guess
    assert res.email_status == "risky"
    assert res.email_confidence == 0.5
    assert calls == ["jane.doe@acme.com"]  # did not blast further permutations


async def test_finder_degrades_to_best_unknown_when_no_valid():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(
            verify=_fake_verify({})
        ).enrich(acc, contact)
    # All unknown -> returns the canonical guess at the unknown verdict confidence.
    assert res.email == "jane.doe@acme.com"
    assert res.email_status == STATUS_UNKNOWN
    assert res.found is True


async def test_finder_not_found_when_no_domain():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}))

    class _C:
        full_name = "Jane Doe"

    class _A:
        domain = None

    res = await prov.enrich(_A(), _C())
    assert res.found is False


async def test_waterfall_pattern_fallback_after_finder_offline():
    """Offline: finder returns unknown; blind pattern (0.4) still wins the email."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()
        enricher = WaterfallEnricher(
            providers=[
                VerifyingPatternEmailProvider(verify=_fake_verify({})),
                PatternEmailProvider(),
            ]
        )
        res = await enricher.enrich_contact(ts, contact, acc)
    assert contact.email == "jane.doe@acme.com"
    assert contact.email_confidence == 0.4


async def test_waterfall_persists_email_status_onto_contact():
    """Regression: the verified deliverability status must land on the contact itself, not just
    the EnrichmentResult — otherwise every enriched contact shows up 'unverified'."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()
        enricher = WaterfallEnricher(
            providers=[
                VerifyingPatternEmailProvider(
                    verify=_fake_verify({"jane.doe@acme.com": (STATUS_VALID, 0.95, {})})
                )
            ]
        )
        await enricher.enrich_contact(ts, contact, acc)
    assert contact.email == "jane.doe@acme.com"
    assert contact.email_status == STATUS_VALID  # the fix: status is persisted, not dropped
