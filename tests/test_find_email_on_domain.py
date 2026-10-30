"""Search another domain: a rep types where this person receives mail (owner decision 2026-10-29).

A company's people sometimes receive mail on a domain that is not its website (a parent group, a
regional brand). The rep knows it; the finder guesses and verifies there for this one person,
leaves the account's own mail domain as it was, and charges one `enrich.contact` only when an
address is kept. Free mail, reserved names, IP addresses and domains with no mail server are
refused before any lookup.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker
from nexus.core.security import decode_access_token
from nexus.enrichment import mail_domain
from nexus.enrichment.providers import VerifyingPatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher, set_enricher
from nexus.models.account import Account, Contact
from nexus.models.billing import BillingUsageEvent
from nexus.verification import STATUS_INVALID, STATUS_VALID, EmailVerification
from tests.conftest import auth, signup, tenant_session


async def _seed_billing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


def _finder(valid: str, probed: list[str]) -> WaterfallEnricher:
    async def verify(email: str) -> EmailVerification:
        probed.append(email)
        status = STATUS_VALID if email == valid else STATUS_INVALID
        return EmailVerification(email=email, status=status, confidence=0.95, source="reacher")

    return WaterfallEnricher(providers=[VerifyingPatternEmailProvider(verify=verify)], verify=verify)


async def _jane(client, slug):
    token = await signup(client, slug=slug, email=f"rep@{slug}.com", company=slug.upper())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme", domain="acme.com",
                          custom_fields={"mail_domain": {"domain": "acme.com",
                                                         "source": "website_domain"}})
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()
        ids = account.id, contact.id
        await ts.session.commit()
    return token, tid, ids


async def _charges(tid):
    async with get_platform_sessionmaker()() as s:
        return len((await s.scalars(select(BillingUsageEvent).where(
            BillingUsageEvent.tenant_id == tid,
            BillingUsageEvent.capability_id == "enrich.contact"))).all())


async def test_an_address_found_on_the_typed_domain_is_kept_and_charged(client, monkeypatch):
    await _seed_billing()
    monkeypatch.setattr(mail_domain, "has_mx", _mx({"acme-group.com"}))
    probed: list[str] = []
    set_enricher(_finder("jane@acme-group.com", probed))
    try:
        token, tid, (account_id, contact_id) = await _jane(client, "fe1")
        r = await client.post(f"/api/accounts/contacts/{contact_id}/find-email",
                              headers=auth(token), json={"domain": "https://www.Acme-Group.com/"})
    finally:
        set_enricher(None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["domain"], body["found"], body["charged"]) == ("acme-group.com", True, True)
    assert body["contact"]["email"] == "jane@acme-group.com"
    assert all(e.endswith("@acme-group.com") for e in probed), "guessed only on the typed domain"
    assert await _charges(tid) == 1
    async with tenant_session(tid) as ts:
        account = await ts.get(Account, account_id)
        contact = await ts.get(Contact, contact_id)
    assert account.custom_fields["mail_domain"]["domain"] == "acme.com", \
        "the account's own mail domain is left as it was"
    assert "acme-group.com" in contact.field_sources["email"]["detail"]


async def test_nothing_found_on_the_typed_domain_costs_nothing(client, monkeypatch):
    await _seed_billing()
    monkeypatch.setattr(mail_domain, "has_mx", _mx({"acme-group.com"}))
    set_enricher(_finder("nobody@elsewhere.com", []))
    try:
        token, tid, (_, contact_id) = await _jane(client, "fe2")
        r = await client.post(f"/api/accounts/contacts/{contact_id}/find-email",
                              headers=auth(token), json={"domain": "acme-group.com"})
    finally:
        set_enricher(None)

    assert r.status_code == 200, r.text
    assert (r.json()["found"], r.json()["charged"]) == (False, False)
    assert "Nothing was charged" in r.json()["message"]
    assert await _charges(tid) == 0


async def test_free_mail_ip_addresses_and_domains_without_mail_are_refused_first(client, monkeypatch):
    await _seed_billing()
    monkeypatch.setattr(mail_domain, "has_mx", _mx(set()))
    token, tid, (_, contact_id) = await _jane(client, "fe3")
    for domain, words in (("gmail.com", "Free mail"), ("10.0.0.1", "company domain"),
                          ("no-mail-here.com", "no mail server")):
        r = await client.post(f"/api/accounts/contacts/{contact_id}/find-email",
                              headers=auth(token), json={"domain": domain})
        assert r.status_code == 422, (domain, r.text)
        assert words in r.text
    assert await _charges(tid) == 0


async def test_another_workspaces_contact_is_not_found(client, monkeypatch):
    await _seed_billing()
    monkeypatch.setattr(mail_domain, "has_mx", _mx({"acme-group.com"}))
    _, _, (_, contact_id) = await _jane(client, "fe4")
    stranger = await signup(client, slug="fe5", email="rep@fe5.com", company="FE5")
    r = await client.post(f"/api/accounts/contacts/{contact_id}/find-email",
                          headers=auth(stranger), json={"domain": "acme-group.com"})
    assert r.status_code == 404


def _mx(domains: set[str]):
    async def has_mx(domain: str) -> bool:
        return domain in domains

    return has_mx
