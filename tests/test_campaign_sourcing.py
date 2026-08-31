"""Campaign contact-sourcing: model fields, schemas, draft retry, send policy. Offline."""
from __future__ import annotations

import pytest

from nexus.models.campaign import (
    SKIP_RISKY,
    SKIP_UNVERIFIED,
    Campaign,
)
from nexus.campaigns.schemas import CampaignIn, CampaignOut
from tests.conftest import make_tenant, tenant_session, seed_relevance_profile


def test_new_skip_reason_constants():
    assert SKIP_UNVERIFIED == "unverified_contact"
    assert SKIP_RISKY == "risky_address"


async def test_campaign_send_risky_defaults_false():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1")
        ts.add(c)
        await ts.flush()
        assert c.send_risky is False


def test_schema_in_accepts_send_risky_default_false():
    body = CampaignIn(name="C", list_id="l1")
    assert body.send_risky is False
    assert CampaignIn(name="C", list_id="l1", send_risky=True).send_risky is True


async def test_schema_out_exposes_send_risky():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1", send_risky=True)
        ts.add(c)
        await ts.flush()
        out = CampaignOut.from_model(c)
    assert out.send_risky is True


from nexus.campaigns.service import CampaignService
from nexus.campaigns.sourcing import set_contact_sourcing_service, ContactSourcingService
from nexus.enrichment.providers import PatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.integrations.contact_search import StubContactSearchProvider
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account, Contact
from nexus.models.campaign import (
    CAMP_AWAITING_APPROVAL,
    TARGET_DRAFTED,
    TARGET_SKIPPED,
)
from nexus.models.workflow import ListItem, ProspectList


async def _list_with_account(ts, *, with_contact=False):
    # Drafting refuses on a workspace with nothing to pitch (RelevanceContext.is_configured).
    await seed_relevance_profile(ts)
    plist = ProspectList(tenant_id=ts.tenant_id, name="L")
    ts.add(plist)
    await ts.flush()
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    if with_contact:
        ts.add(Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe"))
    ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()
    return plist.id


@pytest.fixture(autouse=True)
def _stub_sourcing():
    """Force the deterministic offline sourcing service (stub search + blind pattern)."""
    registry = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    set_contact_sourcing_service(ContactSourcingService(registry=registry, enricher=enricher))
    yield
    set_contact_sourcing_service(None)


async def test_contactless_target_sources_drafts_then_holds_at_send(offline_services):
    async with tenant_session(await make_tenant()) as ts:
        list_id = await _list_with_account(ts, with_contact=False)
        svc = CampaignService()
        campaign = await svc.create(
            ts, name="C", list_id=list_id, icp={"buyer_titles": ["VP Sales"]},
            sequence="seq", created_by_user_id=None,
        )
        await svc.run_draft_phase(ts, campaign)
        assert campaign.status == CAMP_AWAITING_APPROVAL
        targets = await svc.list_targets(ts, campaign.id)
        assert len(targets) == 1
        t = targets[0]
        # A persona was sourced; the draft is grounded and marked sourced.
        assert t.status == TARGET_DRAFTED
        assert t.draft.get("sourced") is True
        assert t.draft.get("contact_id")

        # Send phase: offline the sourced 0.4 guess is unknown & below 0.5 -> held.
        await svc.approve_and_send(ts, campaign, decided_by=None)
        targets = await svc.list_targets(ts, campaign.id)
        assert targets[0].status == TARGET_SKIPPED
        assert targets[0].skip_reason == "unverified_contact"
        assert campaign.report["skips"].get("unverified_contact") == 1


async def test_send_risky_campaign_sends_risky_draft(offline_services):
    svc = CampaignService()
    # A draft graded risky; send_risky=True should let it through the policy.
    risky = {"email_status": "risky", "email_confidence": 0.4, "sourced": True,
             "grounded": True, "contact_id": "c1", "subject": "Hi", "body": "x"}

    class _C:
        send_risky = True

    assert svc._send_policy(risky, _C()) is None

    class _D:
        send_risky = False

    assert svc._send_policy(risky, _D()) == "risky_address"


def test_send_policy_invalid_always_held():
    svc = CampaignService()

    class _C:
        send_risky = True

    assert svc._send_policy({"email_status": "invalid"}, _C()) == "undeliverable_address"


def test_send_policy_real_unknown_contact_sends():
    svc = CampaignService()

    class _C:
        send_risky = False

    # Not sourced (a real existing contact) + unknown -> send, no regression.
    assert svc._send_policy(
        {"email_status": "unknown", "sourced": False}, _C()
    ) is None


async def test_sourced_contacts_never_cross_tenants(offline_services):
    tid_a = await make_tenant(slug="src-a", name="Src A")
    tid_b = await make_tenant(slug="src-b", name="Src B")
    async with tenant_session(tid_a) as ts:
        list_id = await _list_with_account(ts, with_contact=False)
        svc = CampaignService()
        campaign = await svc.create(
            ts, name="A", list_id=list_id, icp={}, sequence="seq",
            created_by_user_id=None,
        )
        await svc.run_draft_phase(ts, campaign)
        a_contacts = await ts.list(Contact)
    # Tenant B sees none of tenant A's sourced personas.
    async with tenant_session(tid_b) as ts:
        b_contacts = await ts.list(Contact)
    assert len(a_contacts) == 1
    assert b_contacts == []
