"""Waterfall enrichment: provider ordering, confidence merging, persistence."""
from __future__ import annotations

from nexus.enrichment.providers import PatternEmailProvider, SearchEnrichmentProvider
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.models.account import Account, Contact
from tests.conftest import FakeBrowser, make_tenant, tenant_session


async def test_pattern_provider_guesses_corporate_email():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()

        enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
        res = await enricher.enrich_contact(ts, contact, acc)
        assert res.email == "jane.doe@acme.com"
        assert contact.email == "jane.doe@acme.com"
        assert contact.enrichment_source == "pattern"


async def test_search_provider_beats_pattern_on_domain_match():
    tid = await make_tenant()
    browser = FakeBrowser([
        {"title": "Jane Doe", "snippet": "reach Jane at jane.doe@acme.com or +1 415-555-1212"}
    ])
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()

        enricher = WaterfallEnricher(
            providers=[SearchEnrichmentProvider(browser), PatternEmailProvider()],
            min_confidence=0.6,
        )
        res = await enricher.enrich_contact(ts, contact, acc)
        assert res.email == "jane.doe@acme.com"
        assert res.email_confidence == 0.8  # domain-matched search beats the 0.4 pattern guess
        assert contact.phone == "+14155551212"


async def test_a_failing_provider_is_skipped():
    class Boom(PatternEmailProvider):
        name = "boom"

        async def enrich(self, account, contact):
            raise RuntimeError("provider down")

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()

        enricher = WaterfallEnricher(providers=[Boom(), PatternEmailProvider()])
        res = await enricher.enrich_contact(ts, contact, acc)
        assert res.email == "jane.doe@acme.com"  # fell through to the working provider
