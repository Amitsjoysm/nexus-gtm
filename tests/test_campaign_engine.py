"""Offline tests for the Segment Campaign Engine (sub-project A)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from nexus.campaigns.service import CampaignService, get_campaign_service
from nexus.core.config import get_settings
from nexus.core.rbac import Permission, Role, has_permission
from nexus.models.account import Account, Contact
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_AWAITING_APPROVAL,
    CAMP_COMPLETED,
    CAMP_DRAFT_PENDING,
    TARGET_DRAFTED,
    TARGET_PENDING,
    TARGET_SENT,
    TARGET_SKIPPED,
    SKIP_NO_CONTACT,
    SKIP_UNDELIVERABLE,
    SKIP_UNGROUNDED,
)
from nexus.models.workflow import ListItem, ProspectList
from nexus.orchestration.planner import available_goals, get_planner
from nexus.verification import STATUS_INVALID
from tests.conftest import make_tenant, tenant_session


@pytest_asyncio.fixture
async def ts():
    """A TenantSession bound to a fresh tenant. The context commits on exit."""
    tid = await make_tenant(slug="camp-a", name="Camp A")
    async with tenant_session(tid) as session:
        yield session


@pytest_asyncio.fixture
async def other_ts():
    """A second TenantSession bound to a different tenant (for isolation tests)."""
    tid = await make_tenant(slug="camp-b", name="Camp B")
    async with tenant_session(tid) as session:
        yield session


async def _make_list_with_accounts(ts, specs: list[dict]) -> str:
    """specs: [{"name", "email"|None}]. Returns the list_id."""
    plist = ProspectList(tenant_id=ts.tenant_id, name="seg", filter={})
    ts.add(plist)
    await ts.flush()
    for spec in specs:
        acc = Account(
            tenant_id=ts.tenant_id,
            name=spec["name"],
            domain=spec["name"].lower() + ".com",
        )
        ts.add(acc)
        await ts.flush()
        if spec.get("email"):
            ts.add(Contact(
                tenant_id=ts.tenant_id, account_id=acc.id,
                full_name="Lead", email=spec["email"],
            ))
        ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()
    return plist.id


async def test_campaign_defaults():
    """Column defaults populate on flush, the codebase idiom for persisted rows."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        c = Campaign(
            tenant_id=tid,
            name="Q3 expansion",
            list_id="list1",
            icp={"industries": ["SaaS"]},
            created_by_user_id="u1",
        )
        ts.add(c)
        await ts.flush()

        assert c.status == CAMP_DRAFT_PENDING
        assert c.sequence == "ai-orchestrated-outbound"
        assert c.report == {}
        assert c.icp == {"industries": ["SaaS"]}


async def test_campaign_target_defaults():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        t = CampaignTarget(tenant_id=tid, campaign_id="c1", account_id="a1")
        ts.add(t)
        await ts.flush()

        assert t.status == TARGET_PENDING
        assert t.skip_reason is None
        assert t.draft == {}


# -- RBAC -----------------------------------------------------------------------------


def test_manage_campaigns_permission_is_manager_plus():
    assert has_permission(Role.manager, Permission.manage_campaigns) is True
    assert has_permission(Role.admin, Permission.manage_campaigns) is True
    assert has_permission(Role.owner, Permission.manage_campaigns) is True
    assert has_permission(Role.rep, Permission.manage_campaigns) is False


# -- planner recipe -------------------------------------------------------------------


def test_research_compose_recipe_has_no_send_step():
    assert "research_compose" in available_goals()
    plan = get_planner().plan("research_compose", {"account_id": "a1"})
    tools = [s["tool"] for s in plan]
    assert tools == ["research", "scoring", "compose_message"]
    # No step requires approval — the draft phase is fully autonomous.
    assert all(s["requires_approval"] is False for s in plan)


# -- config ---------------------------------------------------------------------------


def test_campaign_preview_sample_default():
    assert get_settings().campaign_preview_sample == 3


# -- draft phase ----------------------------------------------------------------------


async def test_draft_phase_drafts_and_reports_skips(ts):
    list_id = await _make_list_with_accounts(
        ts,
        [
            {"name": "Acme", "email": "lead@acme.com"},
            {"name": "NoContactCo", "email": None},
        ],
    )
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)

    assert campaign.status == CAMP_AWAITING_APPROVAL
    targets = await svc.list_targets(ts, campaign.id)
    statuses = sorted(t.status for t in targets)
    assert statuses == [TARGET_DRAFTED, TARGET_SKIPPED]
    skipped = next(t for t in targets if t.status == TARGET_SKIPPED)
    assert skipped.skip_reason == SKIP_NO_CONTACT
    # Report rolls up the counts.
    assert campaign.report["total"] == 2
    assert campaign.report["drafted"] == 1
    assert campaign.report["skipped"] == 1
    assert campaign.report["skips"][SKIP_NO_CONTACT] == 1


def test_classify_skip_reasons():
    """Pure unit test of the skip-reason classifier (no DB, no async).

    Priority order is ungrounded → no-contact → undeliverable; a fully grounded,
    deliverable draft returns None (it is sendable)."""
    classify = CampaignService._classify
    # Ungrounded wins even if a contact/email is present.
    assert classify({"grounded": False, "contact_id": "c1", "email_status": "unknown"}) == SKIP_UNGROUNDED
    # Grounded but no contact, or no email to verify (email_status is None).
    assert classify({"grounded": True, "contact_id": None, "email_status": None}) == SKIP_NO_CONTACT
    assert classify({"grounded": True, "contact_id": "c1", "email_status": None}) == SKIP_NO_CONTACT
    # Grounded, has contact + a verified-invalid address.
    assert classify({"grounded": True, "contact_id": "c1", "email_status": STATUS_INVALID}) == SKIP_UNDELIVERABLE
    # Grounded, deliverable (unknown/valid) → sendable.
    assert classify({"grounded": True, "contact_id": "c1", "email_status": "unknown"}) is None


# -- send phase -----------------------------------------------------------------------


async def test_send_phase_sends_drafted_targets(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    assert campaign.status == CAMP_AWAITING_APPROVAL

    await svc.approve_and_send(ts, campaign, decided_by="u1")

    assert campaign.status == CAMP_COMPLETED
    targets = await svc.list_targets(ts, campaign.id)
    assert all(t.status == TARGET_SENT for t in targets if t.draft)
    assert campaign.report["sent"] >= 1


async def test_approve_requires_awaiting_state(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    # Not yet drafted → cannot approve.
    with pytest.raises(Exception):
        await svc.approve_and_send(ts, campaign, decided_by="u1")


async def test_send_phase_gates_refuse_per_account(ts):
    """Campaign approval is ONE human decision, but it cannot override the per-send hard
    gates. A draft that lost grounding (e.g. an edit) or carries an invalid address is
    refused at the send boundary and lands in the report; the grounded survivor still sends.

    This deliberately mutates target draft snapshots to reach the send-phase gates, because
    the draft phase's own ``_classify`` would otherwise have filtered these cases out before
    they were ever marked DRAFTED — proving the gates are a genuine second line of defense.
    (``Account`` and ``Contact`` are imported at the top of this test module.)
    """
    list_id = await _make_list_with_accounts(
        ts,
        [
            {"name": "Acme", "email": "lead@acme.com"},      # stays grounded+deliverable → SENT
            {"name": "Globex", "email": "lead@globex.com"},  # grounding stripped → refused
            {"name": "Initech", "email": "lead@initech.com"},  # address forced invalid → refused
        ],
    )
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    assert campaign.status == CAMP_AWAITING_APPROVAL

    accounts = {a.name: a for a in await ts.list(Account)}
    targets = {t.account_id: t for t in await svc.list_targets(ts, campaign.id)}
    # All three drafted under the always-grounding stub.
    assert all(targets[a.id].status == TARGET_DRAFTED for a in accounts.values())

    # Strip grounding off Globex's snapshot; ``_send_one`` reasserts ``target.draft`` onto
    # the run blackboard, so the grounded-send gate reads grounded=False and refuses.
    g = targets[accounts["Globex"].id]
    g.draft = {**g.draft, "grounded": False}
    # The verified-send gate re-verifies the LIVE contact address (not the snapshot's cached
    # status), so an invalid verdict must come from the contact record. Break Initech's address.
    initech_contact = await ts.first(Contact, Contact.account_id == accounts["Initech"].id)
    initech_contact.email = "not-an-email"
    await ts.flush()

    await svc.approve_and_send(ts, campaign, decided_by="u1")

    assert campaign.status == CAMP_COMPLETED
    targets = {t.account_id: t for t in await svc.list_targets(ts, campaign.id)}
    assert targets[accounts["Acme"].id].status == TARGET_SENT
    assert targets[accounts["Globex"].id].status == TARGET_SKIPPED
    assert targets[accounts["Globex"].id].skip_reason == SKIP_UNGROUNDED
    assert targets[accounts["Initech"].id].status == TARGET_SKIPPED
    assert targets[accounts["Initech"].id].skip_reason == SKIP_UNDELIVERABLE
    assert campaign.report["sent"] == 1
    assert campaign.report["skips"][SKIP_UNGROUNDED] == 1
    assert campaign.report["skips"][SKIP_UNDELIVERABLE] == 1
