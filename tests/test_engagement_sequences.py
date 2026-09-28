"""Sequences and drafting: campaigns, enrolment, the review queue, the credit gate, and the worker
that writes each follow-up just before it sends (spec §7-§10, D4, D5, D17, D18, D19).

Drafts come from the real `MessagingAgent` on the offline stub model, and sends go to the
`SentFolder` provider double from `test_engagement_sending.py`, installed through the registry seam.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes as _parse
from email.policy import default as _modern

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, make_tenant, seed_relevance_profile, signup, tenant_session
from tests.test_engagement_sending import SentFolder

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)   # a Tuesday


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _world(slug: str, *, contacts: int = 2):
    """A workspace with product context, an SDR, a mailbox, and contacts at an account with a
    live signal — the specific fact every draft is expected to use."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts)
        user = User(email=f"sam@{slug}.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io", country="Germany")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW - timedelta(days=3),
                           dedupe_key=f"{slug}-funding"))
        ids = []
        for i in range(contacts):
            contact = Contact(account_id=account.id, full_name=f"Jane{i} Buyer",
                              email=f"jane{i}@acme.io", title="VP Engineering")
            ts.add(contact)
            await ts.flush()
            ids.append(contact.id)
        return tid, user.id, mailbox.id, ids


STEPS = [
    {"channel": "email", "angle": "Open on the funding round"},
    {"channel": "email", "angle": "A different reason to talk", "delay_business_days": 2},
    {"channel": "email", "angle": "Break-up note", "delay_business_days": 3},
]


# ---- pure rules -----------------------------------------------------------------------------------

def test_steps_are_validated_before_anything_is_saved():
    from nexus.engagement.sequences.service import CampaignError, validate_steps

    clean = validate_steps(STEPS)
    assert clean[0]["delay_business_days"] == 0 and clean[1]["delay_business_days"] == 2
    assert clean[0]["allowed_weekdays"] == [0, 1, 2, 3, 4]
    for bad in ([], [{"channel": "call"}], [{"channel": "sms"}],
                [{"channel": "email"}, {"channel": "email", "timing_mode": "manual"}],
                [{"channel": "email"}, {"channel": "email", "delay_business_days": 99}]):
        with pytest.raises(CampaignError):
            validate_steps(bad)


def test_first_emails_wait_for_approval_and_follow_ups_follow_the_step_timing():
    from types import SimpleNamespace

    from nexus.engagement.sequences.timing import first_due_at, followup_due_at

    approved = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert first_due_at(first_send_mode="on_approval", first_send_at=None,
                        approved_at=approved, now=NOW) == approved
    later = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)
    assert first_due_at(first_send_mode="scheduled", first_send_at=later,
                        approved_at=approved, now=NOW) == later
    # A scheduled time never goes before the approval: scheduling does not approve.
    assert first_due_at(first_send_mode="scheduled", first_send_at=approved - timedelta(days=2),
                        approved_at=approved, now=NOW) == approved

    friday = datetime(2026, 9, 25, 14, 30, tzinfo=UTC)
    auto = SimpleNamespace(timing_mode="auto", delay_business_days=1)
    assert followup_due_at(step=auto, previous_sent_at=friday, zone_name="UTC") == \
        datetime(2026, 9, 28, 14, 30, tzinfo=UTC)          # Monday, same time of day
    manual = SimpleNamespace(timing_mode="manual", delay_business_days=1,
                             send_time_local="09:15", allowed_weekdays=[2])
    assert followup_due_at(step=manual, previous_sent_at=friday, zone_name="UTC") == \
        datetime(2026, 9, 30, 9, 15, tzinfo=UTC)           # next Wednesday at 09:15


def test_a_draft_must_use_a_specific_fact_and_naming_the_company_does_not_count():
    from nexus.agents.email_quality import check_draft
    from nexus.engagement.drafting.personalisation import PROBLEM, uses_a_fact

    facts = ["Acme Robotics raises $40M Series B", "VP Engineering"]
    assert uses_a_fact("Congrats on the $40M raise.", facts, company_name="Acme Robotics")
    assert uses_a_fact("Saw the Series B news.", facts, company_name="Acme Robotics")
    assert not uses_a_fact("Hi Jane, teams at Acme Robotics love us.", facts,
                           company_name="Acme Robotics")
    assert uses_a_fact("anything at all", [], company_name="")
    body = "Hi Jane,\n\nTeams at Acme Robotics love us. Worth a chat?\n\nBest,\nSam"
    assert PROBLEM in check_draft(subject="Hi", body=body, first_name="Jane", facts=facts,
                                  company_name="Acme Robotics")
    # Without facts the check is exactly what it was for every other caller.
    assert PROBLEM not in check_draft(subject="Hi", body=body, first_name="Jane")


# ---- campaigns, enrolment, drafts -----------------------------------------------------------------

async def test_enrolment_skips_who_cannot_be_emailed_and_warns_about_duplicate_outreach():
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact

    tid, user_id, mailbox_id, contact_ids = await _world("enrol", contacts=3)
    async with tenant_session(tid) as ts:
        no_email = await ts.get(Contact, contact_ids[1])
        no_email.email = ""
        blocked = await ts.get(Contact, contact_ids[2])
        await suppress(ts, email=blocked.email, reason="declined")
        first = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        result = await enroll(ts, first, contact_ids + [contact_ids[0]])
        assert result.added == [contact_ids[0]]
        reasons = {s["contact_id"]: s["reason"] for s in result.skipped}
        assert reasons[contact_ids[1]] == "no_email"
        assert reasons[contact_ids[2]] == "do_not_contact:declined"

        second = await create_campaign(ts, name="Webinar follow-up", owner_user_id=user_id,
                                       mailbox_id=mailbox_id, steps=STEPS[:1])
        again = await enroll(ts, second, [contact_ids[0]])
        assert again.added == [contact_ids[0]]
        assert again.warnings[0]["campaign_name"] == "Q4"
        assert again.warnings[0]["owner"] == "Sam Rep"


async def test_the_recipients_timezone_comes_from_their_account_when_they_have_none():
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.models.engagement import EngagementEnrollment

    tid, user_id, mailbox_id, contact_ids = await _world("tz", contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="TZ", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        enrollment = await ts.first(EngagementEnrollment)
        assert enrollment.contact_timezone == "Europe/Berlin"


async def test_first_emails_are_drafted_once_personalised_and_waiting_for_review():
    from nexus.engagement.sequences.service import (
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )

    tid, user_id, mailbox_id, contact_ids = await _world("draft")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        result = await draft_first_emails(ts, campaign, user_id=user_id)
        assert result == {"drafted": 2, "failed": 0, "errors": []}
        assert campaign.status == "reviewing"
        queue = await review_queue(ts, campaign)
        assert len(queue) == 2
        _enrollment, message = queue[0]
        assert message.status == "draft" and message.step_index == 0
        assert "Series B" in message.body_text, "the stub opens on the live signal"
        assert message.quality_problems == []
        assert message.ai_body == message.body_text
        # Idempotent: nothing is drafted twice.
        assert (await draft_first_emails(ts, campaign))["drafted"] == 0


async def test_an_edited_approval_is_recorded_against_the_ai_text():
    from nexus.engagement.ledger import consent
    from nexus.engagement.sequences.service import (
        approve,
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )
    from nexus.models.ledger import LedgerOutbox

    tid, user_id, mailbox_id, contact_ids = await _world("edit", contacts=1)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        (_enrollment, message), = await review_queue(ts, campaign)
        await approve(ts, message, user_id=user_id, body=message.body_text + "\n\nP.S. Congrats.")
        assert message.status == "approved" and message.approved_by_user_id == user_id
        events = {e.event_type: e for e in await ts.list(LedgerOutbox)}
    assert {"draft.created", "draft.edited", "draft.approved"} <= set(events)
    edited = events["draft.edited"].payload["payload"]
    assert edited["ai_body"] != edited["body"] and edited["edit_distance"] == 2


# ---- the gate --------------------------------------------------------------------------------------

async def test_launch_is_refused_when_the_worst_case_cannot_be_covered(monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.credits import grant_credits
    from nexus.billing.rates import sync_rates
    from nexus.engagement.sequences.service import (
        CreditGateRefused,
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid, user_id, mailbox_id, contact_ids = await _world("gate")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        with pytest.raises(CreditGateRefused) as refused:
            await launch(ts, campaign, user_id=user_id)
        estimate = refused.value.estimate
        per = estimate.per_capability
        # 2 contacts x 3 email steps x (draft 2 + send 1) = 18 credits for the emails alone...
        assert per["ai.email_draft"]["credits"] + per["outreach.email_send"]["credits"] == 18
        # ...and the worst case is every priced line, including one reply read per contact.
        assert estimate.worst_credits == sum(line["credits"] for line in per.values())
        assert estimate.gate_applies
        assert estimate.likely_credits < estimate.worst_credits
        await grant_credits(ts, 500, idempotency_key="gate-topup", reason="test")
        result = await launch(ts, campaign, user_id=user_id)
        assert result.covered and campaign.status == "active"


async def test_the_gate_is_skipped_when_billing_is_off(monkeypatch):
    from nexus.engagement.sequences.estimate import estimate
    from nexus.engagement.sequences.service import create_campaign, enroll

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world("gateoff")
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        result = await estimate(ts, campaign)
    assert result.covered and not result.gate_applies


# ---- the worker ------------------------------------------------------------------------------------

async def _launched(slug: str, monkeypatch, **campaign_options):
    from nexus.engagement.sequences.service import (
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world(slug, contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS, **campaign_options)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        await launch(ts, campaign, user_id=user_id)
        return tid, campaign.id


async def _enrollment(tid):
    from nexus.models.engagement import EngagementEnrollment

    async with tenant_session(tid) as ts:
        return await ts.first(EngagementEnrollment)


async def _run(tid, enrollment_id, when):
    from nexus.engagement.sequences.advance import process

    async with tenant_session(tid) as ts:
        return await process(ts, enrollment_id, now=when)


async def test_a_launched_sequence_sends_threads_its_follow_ups_and_completes(folder, monkeypatch):
    from nexus.models.engagement import EngagementMessage

    tid, _campaign_id = await _launched("seq", monkeypatch)
    enrollment = await _enrollment(tid)
    assert enrollment.status == "active" and enrollment.next_action_at is not None

    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == 1 and enrollment.current_thread_id
    first = _parse(folder.delivered[0], policy=_modern)

    # Not due yet: nothing happens.
    assert await _run(tid, enrollment.id, enrollment.next_action_at - timedelta(hours=1)) \
        == "not_due"
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent"
    second = _parse(folder.delivered[1], policy=_modern)
    # The follow-up is written just in time, in the same thread, with exactly one "Re:" (D16).
    assert second["Subject"] == f"Re: {first['Subject']}"
    assert second["In-Reply-To"] == first["Message-ID"]

    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent_completed"
    enrollment = await _enrollment(tid)
    assert enrollment.status == "completed" and enrollment.next_action_at is None
    async with tenant_session(tid) as ts:
        sent = await ts.list(EngagementMessage, EngagementMessage.status == "sent")
    assert sorted(m.step_index for m in sent) == [0, 1, 2]
    assert len(folder.delivered) == 3


async def test_review_every_touch_holds_each_follow_up_for_the_sdr(folder, monkeypatch):
    tid, _ = await _launched("everytouch", monkeypatch, review_every_touch=True)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "awaiting_review"
    assert (await _enrollment(tid)).status == "awaiting_review"
    assert len(folder.delivered) == 1


async def test_a_reply_or_unsubscribe_before_the_step_stops_the_sequence(folder, monkeypatch):
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact

    tid, _ = await _launched("unsub", monkeypatch)
    enrollment = await _enrollment(tid)
    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, enrollment.contact_id)
        await suppress(ts, email=contact.email, reason="unsubscribed")
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "stopped:unsubscribed"
    enrollment = await _enrollment(tid)
    assert enrollment.status == "stopped" and enrollment.status_reason == "unsubscribed"
    assert folder.delivered == []


async def test_a_disconnected_mailbox_pauses_rather_than_failing(folder, monkeypatch):
    from nexus.models.engagement import MailboxConnection

    tid, _ = await _launched("disc", monkeypatch)
    enrollment = await _enrollment(tid)
    async with tenant_session(tid) as ts:
        mailbox = await ts.get(MailboxConnection, enrollment.mailbox_connection_id)
        mailbox.status = "needs_reauth"
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "mailbox_disconnected"
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "mailbox_disconnected")


async def test_the_heartbeat_finds_due_work_only_in_running_campaigns(folder, monkeypatch):
    from nexus.engagement.sequences.advance import due_enrollments
    from nexus.engagement.sequences.service import pause_campaign
    from nexus.models.engagement import EngagementCampaign

    tid, campaign_id = await _launched("due", monkeypatch)
    enrollment = await _enrollment(tid)
    later = enrollment.next_action_at + timedelta(seconds=1)
    assert (tid, enrollment.id) in await due_enrollments(later)
    async with tenant_session(tid) as ts:
        await pause_campaign(ts, await ts.get(EngagementCampaign, campaign_id))
    assert (tid, enrollment.id) not in await due_enrollments(later)


async def test_the_worker_job_is_dark_until_the_switch_is_on(monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue
    from nexus.workers.scheduler import _enqueue_due
    from nexus.workers.tasks import HANDLERS

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert "skipped" in await HANDLERS["advance_engagement"]({})
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = []
    while (job := await queue.dequeue(timeout=0)) is not None:
        names.append(job.name)
    assert "advance_engagement" not in names

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = []
    while (job := await queue.dequeue(timeout=0)) is not None:
        names.append(job.name)
    assert "advance_engagement" in names


# ---- the API ---------------------------------------------------------------------------------------

async def test_the_campaign_api_is_invisible_while_the_engine_is_dark(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="darkapi", email="owner@darkapi.com", company="Dark")
    assert (await client.get("/api/engagement/campaigns", headers=auth(token))).status_code == 404
    assert (await client.get("/api/engagement/templates", headers=auth(token))).status_code == 404


async def test_build_draft_review_and_launch_through_the_api(client, engine_on, monkeypatch):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.signal import SignalEvent
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="flowapi", email="sam@flowapi.com", company="Flow")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await seed_relevance_profile(ts)
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@flowapi.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW, dedupe_key="flowapi-funding"))
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        mailbox_id, contact_id = mailbox.id, contact.id

    template = await client.post("/api/engagement/templates", headers=auth(token), json={
        "name": "Three touches", "steps": STEPS})
    assert template.status_code == 201, template.text
    created = await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Q4", "mailbox_id": mailbox_id, "template_id": template.json()["id"]})
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert [s["step_index"] for s in campaign["steps"]] == [0, 1, 2]

    added = await client.post(f"/api/engagement/campaigns/{campaign['id']}/contacts",
                              headers=auth(token), json={"contact_ids": [contact_id]})
    assert added.json()["added"] == [contact_id]
    drafted = await client.post(f"/api/engagement/campaigns/{campaign['id']}/draft",
                                headers=auth(token))
    assert drafted.json()["drafted"] == 1
    review = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/review",
                               headers=auth(token))).json()
    assert review[0]["status"] == "draft" and review[0]["quality_problems"] == []
    approve = await client.post(f"/api/engagement/messages/{review[0]['message_id']}/approve",
                                headers=auth(token), json={})
    assert approve.status_code == 204, approve.text
    estimate = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/estimate",
                                 headers=auth(token))).json()
    assert estimate["contacts"] == 1 and estimate["email_steps"] == 3
    launched = await client.post(f"/api/engagement/campaigns/{campaign['id']}/launch",
                                 headers=auth(token))
    assert launched.status_code == 200, launched.text
    assert launched.json()["status"] == "active"
    enrollments = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/enrollments",
                                    headers=auth(token))).json()
    assert enrollments[0]["status"] == "active" and enrollments[0]["next_action_at"]


async def test_a_colleague_cannot_see_or_steer_another_reps_campaign(client, engine_on):
    from nexus.engagement.sequences.service import create_campaign
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    token = await signup(client, slug="colleague", email="rep@colleague.com", company="Col")
    from tests.conftest import principal_from_token

    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        other = User(email="other@colleague.com", full_name="Other Rep", password_hash="x")
        ts.session.add(other)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=other.id, provider="google",
                                    email="other@colleague.com", status="connected")
        ts.add(mailbox)
        await ts.flush()
        campaign = await create_campaign(ts, name="Theirs", owner_user_id=other.id,
                                         mailbox_id=mailbox.id, steps=STEPS[:1])
        campaign_id = campaign.id
    # The signer-up is the workspace owner, a manager: they CAN see it. A rep could not; the
    # service-level rule is that the owner or a manager acts on a campaign.
    seen = await client.get(f"/api/engagement/campaigns/{campaign_id}", headers=auth(token))
    assert seen.status_code == 200
    mine = (await client.get("/api/engagement/campaigns", headers=auth(token))).json()
    assert all(c["id"] != campaign_id for c in mine), "the default list is your own campaigns"
