"""Find contacts asks InfoJoy after the shared pool and before a LinkedIn read (2026-10-26).

SDR feedback: contacts "not found" (measured on a probe of 20 companies: LinkedIn failed on all 20,
both Apify keys refused). Decided with the owner: "pool, then InfoJoy, but add others as well for
fallback". InfoJoy is asked for people at the account's own website with the ICP's titles, and
each row must prove it: its company website is the account's domain, its title fits, it is nobody
already held. Its email is the provider's, unverified, so it is checked before it is kept.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, seed_relevance_profile, tenant_session
from tests.prospector_fixtures import FakeVendor

ICP = {"buyer_titles": ["Operations Manager"]}


def _row(first, title, *, url="acme-ops.io", email=None, profile=None):
    return {"FirstName": first, "LastName": "Example", "Title": title,
            "Email": email if email is not None else f"{first.lower()}@acme-ops.io",
            "CompanyName": "Acme Ops", "CompanyURL": url, "Country": "United States",
            "EmployeeLinkedIn": profile or f"https://www.linkedin.com/in/{first.lower()}-example"}


class _LinkedInMustNotBeRead:
    calls = 0

    async def run_actor(self, *args, **kwargs):
        type(self).calls += 1
        from nexus.integrations.apify import ApifyError

        raise ApifyError("refused")


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor(contacts=[
        _row("Ana", "Senior Operations Manager"),
        _row("Ben", "Operations Manager, Field"),
        _row("Cy", "Operations Manager", url="other-company.com"),     # another company
        _row("Di", "Payroll Clerk"),                                    # not a buyer title
    ])
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


@pytest.fixture
def verifier(monkeypatch):
    """The live verifier, answering per address."""
    from nexus.enrichment import reverify
    from nexus.verification.provider import EmailVerification

    verdicts: dict[str, str] = {}

    def fresh_verify():
        async def verify(email):
            return EmailVerification(email=email, status=verdicts.get(email, "valid"),
                                     confidence=0.95, source="reacher")
        return verify

    monkeypatch.setattr(reverify, "fresh_verify", fresh_verify)
    return verdicts


async def _find(slug, *, limit=2, linkedin=None):
    from nexus.contacts.sourcing import find_contacts
    from nexus.models.account import Account

    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
        account = Account(tenant_id=tid, name="Acme Ops", domain="acme-ops.io")
        ts.add(account)
        await ts.flush()
        result = await find_contacts(ts, account, limit=limit,
                                     linkedin_client=linkedin or _LinkedInMustNotBeRead())
        return result, [(c.full_name, c.enrichment_source, c.email, dict(c.field_sources or {}))
                        for c in result.contacts]


async def test_infojoy_people_at_this_company_in_a_buyer_title_are_added(vendor, verifier):
    _LinkedInMustNotBeRead.calls = 0
    result, people = await _find("ij1")

    assert [(n, s, e) for n, s, e, _ in people] == [
        ("Ana Example", "sourcing:infojoy", "ana@acme-ops.io"),
        ("Ben Example", "sourcing:infojoy", "ben@acme-ops.io")]
    sources = people[0][3]
    assert sources["person"]["source"] == "infojoy"
    assert sources["email"]["source"] == "infojoy"
    assert "valid, checked at the mailbox" in sources["email"]["detail"]

    outcome = result.outcome["infojoy"]
    assert outcome["status"] == "found" and outcome["found"] == 2
    assert outcome["discarded"] == {"wrong_company": 1, "wrong_title": 1}
    assert result.message == "Added 2 people from InfoJoy, each with a verified email."
    # Filled from InfoJoy, so no LinkedIn run was bought.
    assert _LinkedInMustNotBeRead.calls == 0

    sent = vendor.requests[0]["body"]["Filters"]
    assert sent["CompanyUrls"] == ["acme-ops.io"] and "Operations Manager" in sent["Titles"]


async def test_an_address_the_verifier_disproves_is_not_kept(vendor, verifier):
    verifier["ana@acme-ops.io"] = "invalid"

    result, people = await _find("ij2")

    ana = next(p for p in people if p[0] == "Ana Example")
    assert ana[2] != "ana@acme-ops.io"
    contact = next(c for c in result.contacts if c.full_name == "Ana Example")
    assert "ana@acme-ops.io" in (contact.custom_fields or {}).get("rejected_emails", [])


async def test_infojoy_down_is_said_and_linkedin_is_tried(vendor, verifier):
    vendor.status = 500
    _LinkedInMustNotBeRead.calls = 0

    result, people = await _find("ij3")

    assert people == []
    assert result.outcome["infojoy"]["status"] == "unavailable"
    assert "InfoJoy is unavailable right now." in result.message
    # The next source was asked (and refused, as on the probe).
    assert _LinkedInMustNotBeRead.calls >= 1
    assert result.outcome["linkedin"]["status"] == "unavailable"


async def test_without_an_infojoy_key_nothing_mentions_it():
    result, _ = await _find("ij4")

    assert result.outcome["infojoy"]["status"] == "not_configured"
    assert "InfoJoy" not in result.message


# ---- contact-first discovery: a company is added once a buyer is found there --------------------

async def _deliver(slug, icp, domains):
    from collections import Counter

    from nexus.prospecting.companies import Candidate
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=icp)
    discarded = Counter()
    async with tenant_session(tid) as ts:
        cands = [Candidate(domain=d, name=d, source="linkedin", industry="Oil and Gas",
                           linkedin_url=f"https://www.linkedin.com/company/{d}/") for d in domains]
        ids, _ = await deliver(ts, await get_profile(ts), cands, limit=5,
                               source="auto_discovery", min_fit=0, discarded=discarded)
        from nexus.models.account import Account

        return sorted([(await ts.get(Account, i)).domain for i in ids]), discarded


async def test_require_buyer_adds_only_companies_with_someone_in_the_titles(vendor, monkeypatch):
    import json

    import httpx

    from nexus.core.config import get_settings
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient
    from tests.prospector_fixtures import page

    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    ana = [_row("Ana", "Operations Manager")]          # works at acme-ops.io, and only there

    def only_acme(request):
        filters = json.loads(request.content)["Filters"]
        rows = ana if filters["CompanyUrls"] == ["acme-ops.io"] else []
        return httpx.Response(200, json=page(rows, number=1, size=15, total=None))

    search.set_client(InfoJoyClient(keys=["k"], transport=httpx.MockTransport(only_acme),
                                    backoff_s=0))
    domains, discarded = await _deliver(
        "ij-buyer1", {**ICP, "require_buyer": True}, ["acme-ops.io", "nobody-here.io"])

    assert domains == ["acme-ops.io"]
    assert discarded["no_buyer"] == 1


async def test_without_require_buyer_nothing_is_checked(vendor, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    domains, discarded = await _deliver("ij-buyer2", ICP, ["acme-ops.io", "nobody-here.io"])

    assert domains == ["acme-ops.io", "nobody-here.io"] and vendor.requests == []
