"""An address proven invalid is never kept — on Re-verify, on the bulk check, or by the finder.

Decided with the product owner 2026-09-22, after customers reported major deliverability failures:
an invalid address is removed from the contact, not relabelled, and remembered so it is never
guessed again. A relabelled dead address still reads as "an email was found", and a rep with a
deadline sends it.
"""
from __future__ import annotations

from nexus.enrichment.providers import VerifyingPatternEmailProvider
from nexus.enrichment.reverify import reverify_contacts, reverify_or_find
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_INVALID, STATUS_RISKY, STATUS_VALID, EmailVerification
from tests.conftest import make_tenant, tenant_session


def _verdict(status, source="reacher"):
    async def verify(email: str) -> EmailVerification:
        return EmailVerification(email=email, status=status, confidence=0.95, source=source)

    return verify


async def _contact(ts, tid, email=None, status=None):
    acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", email=email,
                email_status=status, email_confidence=0.8 if email else 0.0)
    ts.add(c)
    await ts.flush()
    return c


async def test_reverify_removes_a_disproved_address_and_never_probes_it_again(monkeypatch):
    import nexus.billing.meter as meter_mod
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def free(*a, **k):
        yield None

    monkeypatch.setattr(meter_mod, "metered", free)
    probed: list[str] = []

    async def finder_verify(email: str) -> EmailVerification:
        probed.append(email)
        return EmailVerification(email=email, status=STATUS_INVALID, confidence=0.95,
                                 source="reacher")

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _contact(ts, tid, email="jane.doe@acme.com", status=STATUS_RISKY)
        enricher = WaterfallEnricher(
            providers=[VerifyingPatternEmailProvider(verify=finder_verify)], verify=finder_verify,
        )
        await reverify_or_find(ts, contact, verify=_verdict(STATUS_INVALID), enricher=enricher)

    assert contact.email is None, "a proven-invalid address was kept"
    assert "jane.doe@acme.com" in contact.custom_fields["rejected_emails"]
    assert "jane.doe@acme.com" not in probed, "the search probed the address just disproved"


async def test_the_bulk_check_removes_invalid_addresses_and_counts_them():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _contact(ts, tid, email="jane.doe@acme.com", status=None)
        result = await reverify_contacts(ts, verify=_verdict(STATUS_INVALID))

    assert contact.email is None
    assert "jane.doe@acme.com" in contact.custom_fields["rejected_emails"]
    assert result["statuses"].get(STATUS_INVALID) == 1


async def test_the_bulk_check_keeps_a_valid_address():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _contact(ts, tid, email="jane.doe@acme.com", status=None)
        await reverify_contacts(ts, verify=_verdict(STATUS_VALID))

    assert contact.email == "jane.doe@acme.com"
    assert contact.email_status == STATUS_VALID


async def test_the_finder_never_tries_a_remembered_address():
    tried: list[str] = []

    async def verify(email: str) -> EmailVerification:
        tried.append(email)
        return EmailVerification(email=email, status=STATUS_INVALID, confidence=0.95,
                                 source="reacher")

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _contact(ts, tid)
        contact.custom_fields = {"rejected_emails": ["jane.doe@acme.com"]}
        account = await ts.get(Account, contact.account_id)
        await VerifyingPatternEmailProvider(verify=verify).enrich(account, contact)

    assert "jane.doe@acme.com" not in tried
    assert tried, "the other patterns should still be tried"
