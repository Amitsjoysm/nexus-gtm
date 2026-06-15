"""ContactSourcingService.ensure_contact: create persona / fill email / no-candidate. Offline."""
from __future__ import annotations

from nexus.campaigns.sourcing import ContactSourcingService, SourcingOutcome
from nexus.enrichment.providers import PatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.integrations.contact_search import StubContactSearchProvider
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account, Contact
from tests.conftest import make_tenant, tenant_session


def _service() -> ContactSourcingService:
    registry = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    return ContactSourcingService(registry=registry, enricher=enricher)


async def test_zero_contact_account_sources_persona_and_email():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={"buyer_titles": ["VP Sales"]})
    assert isinstance(outcome, SourcingOutcome)
    assert outcome.sourced is True
    assert outcome.contact is not None
    assert outcome.contact.full_name == "Acme VP Sales"
    assert outcome.contact.title == "VP Sales"
    assert outcome.contact.email == "acme.sales@acme.com"
    assert outcome.contact.enrichment_source.startswith("sourcing:")
    assert outcome.email_confidence == 0.4


async def test_emailless_existing_contact_filled_in_place():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe")
        ts.add(c)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={})
    assert outcome.sourced is True
    assert outcome.contact.id == c.id           # same row, filled in place
    assert outcome.contact.email == "jane.doe@acme.com"


async def test_existing_contact_with_email_is_returned_not_re_sourced():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe",
                    email="jane@acme.com", email_confidence=0.9)
        ts.add(c)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={})
    assert outcome.contact.id == c.id
    assert outcome.sourced is False
    assert outcome.email_confidence == 0.9


async def test_no_candidate_returns_empty_outcome():
    class Empty(StubContactSearchProvider):
        async def search(self, account, icp, *, limit=3):
            return []

    registry = DataSourceRegistry(contact_search=[Empty()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    svc = ContactSourcingService(registry=registry, enricher=enricher)
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        outcome = await svc.ensure_contact(ts, acc, icp={})
    assert outcome.contact is None
    assert outcome.sourced is False
    assert outcome.email_confidence == 0.0
