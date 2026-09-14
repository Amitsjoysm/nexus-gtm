"""Re-verify one contact: re-check the saved address first, search patterns only if it failed.

Decided with the product owner 2026-09-14:

* Re-check the address already on the contact against the live verifier. If it still holds up
  (valid, risky or catch-all), keep it and charge ONE email check (`verify.email`).
* If it comes back invalid or unknown, or there is no address, run the pattern search — first.last,
  then first, and so on, stopping at the first valid — and charge ONE contact enrichment
  (`enrich.contact`) INSTEAD. Never both.
* The Enrich button is unchanged; this is the explicit "ask again" a user reaches for.

All offline: verifiers are fakes and the billing meter is recorded rather than run.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import nexus.billing.meter as meter_mod
from nexus.billing.errors import QuotaExceeded
from nexus.core.security import decode_access_token
from nexus.enrichment.providers import VerifyingPatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher, set_enricher
from nexus.models.account import Account, Contact
from nexus.verification import (
    STATUS_CATCH_ALL,
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from tests.conftest import auth, make_tenant, signup, tenant_session

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _record_charges(monkeypatch, *, refuse: str | None = None) -> list[tuple[str, float]]:
    """Replace the billing meter with a recorder. ``refuse`` names a capability to block with 402."""
    charges: list[tuple[str, float]] = []

    @asynccontextmanager
    async def fake_metered(ts, capability_id, *, quantity=1, **_kw):
        if capability_id == refuse:
            raise QuotaExceeded(capability_id, reason="quota_exhausted")
        charges.append((capability_id, quantity))
        yield None

    monkeypatch.setattr(meter_mod, "metered", fake_metered)
    return charges


def _recheck(status: str):
    """The live re-check of the saved address."""
    calls: list[str] = []

    async def verify(email: str) -> EmailVerification:
        calls.append(email)
        return EmailVerification(email=email, status=status, confidence=0.95, source="reacher")

    verify.calls = calls  # type: ignore[attr-defined]
    return verify


def _must_not_run(what: str):
    async def boom(*_a, **_kw):
        raise AssertionError(f"{what} should not have run")

    return boom


def _finder_enricher(probed: list[str]) -> WaterfallEnricher:
    """The real waterfall around the real pattern finder, over what the live verifier said about
    marketjoy.com on 2026-09-14: curtis@ is a real mailbox, curtis.bent@ is not."""

    async def verify(email: str) -> EmailVerification:
        probed.append(email)
        status = STATUS_VALID if email == "curtis@marketjoy.com" else STATUS_INVALID
        return EmailVerification(email=email, status=status, confidence=0.95, source="reacher")

    return WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)], verify=verify)


async def _curtis(ts, *, email=None, status=None, confidence=0.0) -> Contact:
    acc = Account(tenant_id=ts.tenant_id, name="Marketjoy", domain="marketjoy.com")
    ts.add(acc)
    await ts.flush()
    contact = Contact(
        tenant_id=ts.tenant_id, account_id=acc.id, full_name="Curtis Bent", title="CRO",
        email=email, email_status=status, email_confidence=confidence,
    )
    ts.add(contact)
    await ts.flush()
    return contact


# ---- the saved address still holds up ------------------------------------------------------------

@pytest.mark.parametrize("status", [STATUS_VALID, STATUS_RISKY, STATUS_CATCH_ALL])
async def test_a_saved_address_that_holds_up_is_kept_and_charged_one_email_check(monkeypatch, status):
    from nexus.enrichment.reverify import reverify_or_find

    charges = _record_charges(monkeypatch)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _curtis(ts, email="curtis@marketjoy.com", status=STATUS_UNKNOWN, confidence=0.4)
        enricher = WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=_must_not_run("search"))])

        outcome = await reverify_or_find(ts, contact, verify=_recheck(status), enricher=enricher)

    assert outcome.action == "rechecked"
    assert contact.email == "curtis@marketjoy.com"
    assert contact.email_status == status
    assert charges == [("verify.email", 1)]


# ---- the saved address failed: search patterns, charged as the enrichment instead ----------------

async def test_an_invalid_saved_address_searches_patterns_and_charges_the_enrichment_instead(monkeypatch):
    from nexus.enrichment.reverify import reverify_or_find

    charges = _record_charges(monkeypatch)
    probed: list[str] = []
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        # A confident guess from earlier (a web hit, or a DNS-fallback "risky"): it must not outrank
        # the address the search proves, once the re-check has shown it is wrong.
        contact = await _curtis(ts, email="curtis.bent@marketjoy.com", status=STATUS_RISKY, confidence=0.8)

        outcome = await reverify_or_find(
            ts, contact, verify=_recheck(STATUS_INVALID), enricher=_finder_enricher(probed)
        )

    assert outcome.action == "searched"
    assert (outcome.previous_email, outcome.previous_status) == ("curtis.bent@marketjoy.com", STATUS_RISKY)
    assert (contact.email, contact.email_status) == ("curtis@marketjoy.com", STATUS_VALID)
    # first.last, then first — and it stops at the first valid instead of probing the other eight.
    assert probed == ["curtis.bent@marketjoy.com", "curtis@marketjoy.com"]
    assert charges == [("enrich.contact", 1)]


async def test_an_unknown_recheck_also_searches(monkeypatch):
    from nexus.enrichment.reverify import reverify_or_find

    charges = _record_charges(monkeypatch)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _curtis(ts, email="curtis.bent@marketjoy.com", status=STATUS_RISKY, confidence=0.5)

        outcome = await reverify_or_find(
            ts, contact, verify=_recheck(STATUS_UNKNOWN), enricher=_finder_enricher([])
        )

    assert outcome.action == "searched"
    assert (contact.email, contact.email_status) == ("curtis@marketjoy.com", STATUS_VALID)
    assert charges == [("enrich.contact", 1)]


async def test_a_contact_without_an_email_goes_straight_to_the_search(monkeypatch):
    from nexus.enrichment.reverify import reverify_or_find

    charges = _record_charges(monkeypatch)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _curtis(ts)

        outcome = await reverify_or_find(
            ts, contact, verify=_must_not_run("re-check"), enricher=_finder_enricher([])
        )

    assert outcome.action == "searched"
    assert (contact.email, contact.email_status) == ("curtis@marketjoy.com", STATUS_VALID)
    assert charges == [("enrich.contact", 1)]


async def test_a_refused_email_check_leaves_the_contact_as_it_was(monkeypatch):
    from nexus.enrichment.reverify import reverify_or_find

    _record_charges(monkeypatch, refuse="verify.email")
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        contact = await _curtis(ts, email="curtis@marketjoy.com", status=STATUS_UNKNOWN, confidence=0.4)

        with pytest.raises(QuotaExceeded):
            await reverify_or_find(ts, contact, verify=_recheck(STATUS_VALID), enricher=_finder_enricher([]))

    assert contact.email_status == STATUS_UNKNOWN  # no verdict delivered without the charge


# ---- the endpoint --------------------------------------------------------------------------------

async def test_the_endpoint_reports_what_it_did(client, monkeypatch):
    import nexus.enrichment.reverify as reverify_mod

    charges = _record_charges(monkeypatch)
    monkeypatch.setattr(reverify_mod, "fresh_verify", lambda: _recheck(STATUS_INVALID))
    set_enricher(_finder_enricher([]))
    try:
        token = await signup(client, slug="rv1", email="o@rv1.x", company="RV1")
        tid = (decode_access_token(token) or {})["tid"]
        async with tenant_session(tid) as ts:
            contact = await _curtis(ts, email="curtis.bent@marketjoy.com", status=STATUS_RISKY, confidence=0.5)
            contact_id = contact.id

        r = await client.post(f"/api/accounts/contacts/{contact_id}/reverify", headers=auth(token))
    finally:
        set_enricher(None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "searched"
    assert body["previous_email"] == "curtis.bent@marketjoy.com"
    assert body["contact"]["email"] == "curtis@marketjoy.com"
    assert body["contact"]["email_status"] == STATUS_VALID
    assert charges == [("enrich.contact", 1)]


# ---- the button ----------------------------------------------------------------------------------

def test_each_contact_offers_re_verify_on_both_pages():
    api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
    assert re.search(r"reverifyContact\(contactId", api), "no single-contact re-verify in the client"
    assert "/accounts/contacts/${contactId}/reverify" in api

    for page in ("pages/ContactsPage.tsx", "pages/AccountDetailPage.tsx"):
        source = (FRONTEND / page).read_text(encoding="utf-8")
        assert "api.reverifyContact(" in source, f"{page} has no per-contact Re-verify"
