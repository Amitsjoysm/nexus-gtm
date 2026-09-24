"""The SDR enhancements (spec §19): referral follow-through, CRM activity logging, and signal
re-engagement.

Real rows through `tenant_session`, the real send path through the provider double the other
engagement suites use, and the CRM through `StubCRMConnector`, the connector's own offline double
installed through the documented `set_crm_connector` seam (D21).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import NOW, _enrollment, _launched, _run, _world


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


# ---- referral: who a reply points to (pure) ------------------------------------------------------

def test_a_referral_is_read_from_the_cc_line_the_text_and_the_phrases_people_use():
    from nexus.engagement.enhancements.referral import extract

    found = extract(
        "Thanks, I'm not the right person. Please talk to Priya Shah (priya.shah@acme.io); "
        "Omar Farouk is the right person for billing. Also loop in Marketing.",
        cc=["dev.lee@acme.io", "sam@seller.io"], sender="jane@acme.io", ours="sam@seller.io",
        account_domain="acme.io", exclude_names=("Jane Buyer", "Sam Rep"))
    assert [(p.name, p.email, p.evidence) for p in found] == [
        ("Dev Lee", "dev.lee@acme.io", "copied on the reply"),
        ("Priya Shah", "priya.shah@acme.io", "named in the reply"),
        ("Omar Farouk", "", "named in the reply"),
    ]


def test_addresses_elsewhere_the_sender_and_departments_are_not_referrals():
    from nexus.engagement.enhancements.referral import extract

    found = extract("Reach out to Jane at jane@acme.io, or my friend at bob@gmail.com. "
                    "Contact Sales if not. Talk to me first.",
                    sender="jane@acme.io", account_domain="acme.io", exclude_names=("Jane Buyer",))
    assert found == [], "the sender, another domain, a department and 'me' name nobody new"


def test_a_name_is_read_from_an_address_only_when_the_address_spells_one():
    from nexus.engagement.enhancements.referral import name_from_email

    assert name_from_email("priya.shah@acme.io") == "Priya Shah"
    assert name_from_email("p_shah@acme.io") == "P Shah"
    assert name_from_email("pshah@acme.io") == ""


# ---- referral: follow-through (real rows, real enrollment, real draft) ---------------------------

async def _referral_reply(tid, *, body: str, cc: list[str]):
    from nexus.core.db import utcnow
    from nexus.models.engagement import (
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        ReplyClassification,
    )

    async with tenant_session(tid) as ts:
        enrollment = await ts.first(EngagementEnrollment)
        thread = await ts.first(EngagementThread)
        inbound = EngagementMessage(
            mailbox_connection_id=enrollment.mailbox_connection_id, thread_id=thread.id,
            contact_id=enrollment.contact_id, direction="in", kind="reply", status="received",
            from_addr="jane0@acme.io", cc_addrs=cc, subject="Re: Quick question",
            body_text=body, received_at=utcnow())
        ts.add(inbound)
        await ts.flush()
        reading = ReplyClassification(
            message_id=inbound.id, mailbox_connection_id=enrollment.mailbox_connection_id,
            enrollment_id=enrollment.id, contact_id=enrollment.contact_id,
            account_id=enrollment.account_id, category="referral", confidence=0.9)
        ts.add(reading)
        await ts.flush()
        return reading.id, enrollment.campaign_id


async def test_a_referral_becomes_an_intro_waiting_in_the_review_queue(folder, monkeypatch):
    from nexus.engagement.drafting.context import build_context
    from nexus.engagement.enhancements import referral
    from nexus.engagement.sequences.service import review_queue
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementCampaign, MailboxConnection, ReplyClassification

    tid, _ = await _launched("referral", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    reading_id, campaign_id = await _referral_reply(
        tid, cc=["priya.shah@acme.io"],
        body="I'm not the right person, please talk to Priya Shah, she runs platform.\n\n"
             "On Tue, Sam Rep wrote:\n> Could we talk to Bob Smith about it?")

    async with tenant_session(tid) as ts:
        reading = await ts.get(ReplyClassification, reading_id)
        named = await referral.candidates(ts, reading)
        # The quoted history names Bob Smith; that was us, not them.
        assert [(c["name"], c["email"], c["contact_id"]) for c in named] == [
            ("Priya Shah", "priya.shah@acme.io", None)]
        result = await referral.follow_through(ts, reading, name="Priya Shah",
                                               email="priya.shah@acme.io", user_id="u1")
    assert result["campaign_id"] == campaign_id and result["drafted"], result

    async with tenant_session(tid) as ts:
        priya = await ts.get(Contact, result["contact_id"])
        assert priya.custom_fields["referred_by"]["name"] == "Jane0 Buyer"
        campaign = await ts.get(EngagementCampaign, campaign_id)
        waiting = {e.contact_id: row for e, row in await review_queue(ts, campaign)}
        assert waiting[priya.id] is not None and waiting[priya.id].status == "draft"
        pack = await build_context(
            ts, enrollment=next(e for e, _ in await review_queue(ts, campaign)
                                if e.contact_id == priya.id),
            contact=priya, account=await ts.get(Account, priya.account_id),
            mailbox=await ts.get(MailboxConnection, campaign.mailbox_connection_id), kind="first")
        assert "REFERRAL\n- Jane0 Buyer (VP Engineering) at Acme Robotics suggested" in pack.text
        assert "Jane0 suggested you get in touch" in pack.text
        # Their words never reach the prompt; only who made the introduction.
        assert "runs platform" not in pack.text

        reading = await ts.get(ReplyClassification, reading_id)
        with pytest.raises(referral.ReferralError, match="already in Q4"):
            await referral.follow_through(ts, reading, name="Priya", email="", user_id="u1")
        with pytest.raises(referral.ReferralError, match="Add Omar's surname"):
            await referral.follow_through(ts, reading, name="Omar", email="", user_id="u1")


async def test_a_campaign_with_no_steps_refuses_the_intro_before_creating_anyone(folder,
                                                                                monkeypatch):
    from nexus.engagement.enhancements import referral
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementStep, ReplyClassification

    tid, _ = await _launched("referralnosteps", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    reading_id, _campaign_id = await _referral_reply(tid, cc=["dev.lee@acme.io"],
                                                     body="Please talk to Dev Lee.")
    async with tenant_session(tid) as ts:
        # A hand-built or migrated campaign can reach here without a step.
        for step in await ts.list(EngagementStep):
            await ts.delete(step)
    async with tenant_session(tid) as ts:
        reading = await ts.get(ReplyClassification, reading_id)
        with pytest.raises(referral.ReferralError, match="has no steps"):
            await referral.follow_through(ts, reading, name="Dev Lee", email="dev.lee@acme.io",
                                          user_id="u1")
        assert await ts.first(Contact, Contact.email == "dev.lee@acme.io") is None


# ---- CRM activity logging ------------------------------------------------------------------------

async def test_sends_replies_and_meetings_reach_the_crm_once(monkeypatch):
    from nexus.core.db import utcnow
    from nexus.ingestion.crm import StubCRMConnector, set_crm_connector
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, ReplyClassification
    from nexus.models.identity import Tenant
    from nexus.workers.tasks import handle_log_engagement_crm

    tid, _user_id, mailbox_id, (jane, ken) = await _world("crmlog", contacts=2)
    now = utcnow()
    async with tenant_session(tid) as ts:
        (await ts.session.get(Tenant, tid)).automation_enabled = True
        acme = await ts.first(Account)
        acme.crm_id, acme.crm_source = "9001", "stub"
        globex = Account(name="Globex", domain="globex.com")
        ts.add(globex)
        await ts.flush()
        stranger = Contact(account_id=globex.id, full_name="Gia Ray", email="gia@globex.com")
        ts.add(stranger)
        await ts.flush()

        def message(contact_id, direction, **fields):
            row = EngagementMessage(mailbox_connection_id=mailbox_id, contact_id=contact_id,
                                    direction=direction, subject="Quick question", **fields)
            ts.add(row)
            return row

        message(jane, "out", kind="step", status="sent", sent_at=now - timedelta(hours=2))
        reply = message(jane, "in", kind="reply", status="received",
                        received_at=now - timedelta(hours=1))
        away = message(ken, "in", kind="reply", status="received",
                       received_at=now - timedelta(minutes=30))
        message(jane, "out", kind="step", status="sent", sent_at=now - timedelta(days=9))
        message(stranger.id, "out", kind="step", status="sent", sent_at=now - timedelta(hours=3))
        await ts.flush()
        ts.add(ReplyClassification(message_id=reply.id, mailbox_connection_id=mailbox_id,
                                   contact_id=jane, account_id=acme.id, category="interested",
                                   decision="meeting", decided_at=now - timedelta(minutes=10),
                                   status="done"))
        ts.add(ReplyClassification(message_id=away.id, mailbox_connection_id=mailbox_id,
                                   contact_id=ken, account_id=acme.id,
                                   category="out_of_office"))

    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    connector = StubCRMConnector()
    set_crm_connector(connector)
    try:
        first = await handle_log_engagement_crm({})
        # Globex is not in the CRM yet, so its email waits; the nine-day-old send is not backfilled;
        # the out-of-office is not a reply.
        assert first == {"tenants": 1, "logged": 3, "waiting": 1, "failed": 0}
        assert [a["kind"] for a in connector.pushed_activities] == \
            ["email_sent", "email_reply", "meeting_booked"]
        assert {a["account_id"] for a in connector.pushed_activities} == {"9001"}
        assert connector.pushed_activities[0]["detail"]["subject"] == \
            "Email to Jane0 Buyer: Quick question"
        assert connector.pushed_activities[2]["detail"]["subject"] == \
            "Meeting booked with Jane0 Buyer"

        again = await handle_log_engagement_crm({})
        assert again["logged"] == 0 and len(connector.pushed_activities) == 3
    finally:
        set_crm_connector(None)


async def test_the_crm_log_waits_for_its_switches(monkeypatch):
    from nexus.workers.tasks import handle_log_engagement_crm

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    assert await handle_log_engagement_crm({}) == {"skipped": "crm_sync_disabled"}
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert "skipped" in await handle_log_engagement_crm({})


# ---- signal re-engagement ------------------------------------------------------------------------

async def test_news_suggests_writing_again_only_to_people_it_is_fair_to_write_to():
    from nexus.engagement.enhancements.signal_reengage import suggestions
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment
    from tests.test_engagement_sequences import STEPS

    tid, user_id, mailbox_id, ids = await _world("restartrules", contacts=7)
    # The world's signal: "Acme Robotics raises $40M Series B", three days before NOW.
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, ids)
        rows = {e.contact_id: e for e in await ts.list(EngagementEnrollment)}
        quiet, declined, soon, far, blocked, recent, elsewhere = (rows[i] for i in ids)
        # Went quiet here, but another campaign is about to email them anyway.
        other = await create_campaign(ts, name="Q1", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, other, [elsewhere.contact_id])
        for e in (quiet, blocked, elsewhere):
            e.status, e.finished_at = "completed", NOW - timedelta(days=10)
        declined.status, declined.status_reason = "stopped", "declined"
        soon.status, soon.status_reason = "snoozed", "later"
        soon.snoozed_until = NOW + timedelta(days=5)
        far.status, far.status_reason = "snoozed", "later"
        far.snoozed_until = NOW + timedelta(days=60)
        # Went quiet AFTER the news: the news is not a new reason to write.
        recent.status, recent.finished_at = "completed", NOW - timedelta(days=1)
        await suppress(ts, email=(await ts.get(Contact, blocked.contact_id)).email,
                       reason="manual")

    async with tenant_session(tid) as ts:
        found = await suggestions(ts, user_id=user_id, now=NOW)
    assert {(s.contact_id, s.reason) for s in found} == {(quiet.contact_id, "quiet"),
                                                        (far.contact_id, "later")}
    assert {s.signal_title for s in found} == {"Acme Robotics raises $40M Series B"}


async def test_writing_again_drafts_in_the_thread_and_sends_once(folder, monkeypatch):
    from email import message_from_bytes

    from nexus.core.db import utcnow
    from nexus.engagement.enhancements import signal_reengage
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage, MailboxConnection
    from nexus.models.signal import SignalEvent

    tid, _ = await _launched("restartsend", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    now = utcnow()
    async with tenant_session(tid) as ts:
        row = await ts.get(EngagementEnrollment, enrollment.id)
        row.status, row.finished_at = "completed", now - timedelta(days=18)
        for sent in await ts.list(EngagementMessage):
            sent.sent_at = now - timedelta(days=20)
        news = SignalEvent(account_id=row.account_id, kind="hiring", source="ats",
                           title="Acme Robotics is hiring 12 platform engineers", strength=0.95,
                           occurred_at=now - timedelta(days=2), dedupe_key="restartsend-hiring")
        ts.add(news)
        await ts.flush()
        user_id = (await ts.get(MailboxConnection, row.mailbox_connection_id)).owner_user_id
        signal_id = news.id

    async with tenant_session(tid) as ts:
        found = await signal_reengage.suggestions(ts, user_id=user_id, now=now)
        assert [(s.enrollment_id, s.signal_id) for s in found] == [(enrollment.id, signal_id)]
        written = await signal_reengage.draft(ts, user_id=user_id, enrollment_id=enrollment.id,
                                              signal_id=signal_id, now=now)
    assert written["subject"].startswith("Re: ") and written["body"]

    async with tenant_session(tid) as ts:
        result = await signal_reengage.send(ts, user_id=user_id, enrollment_id=enrollment.id,
                                            signal_id=signal_id, subject=written["subject"],
                                            body=written["body"], now=now)
    assert result.sent, result
    first, again = (message_from_bytes(raw) for raw in folder.delivered)
    assert again["In-Reply-To"] == first["Message-ID"], "in the same thread"

    async with tenant_session(tid) as ts:
        assert await signal_reengage.suggestions(ts, user_id=user_id, now=now) == []
        with pytest.raises(signal_reengage.RestartError, match="no longer applies"):
            await signal_reengage.send(ts, user_id=user_id, enrollment_id=enrollment.id,
                                       signal_id=signal_id, subject="x", body="y", now=now)
    assert len(folder.delivered) == 2


# ---- the routes ----------------------------------------------------------------------------------

async def test_the_enhancements_are_dark_with_the_engine_and_answer_with_it(client, monkeypatch):
    token = await signup(client, slug="enhdark", email="sam@enhdark.com", company="E")
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert (await client.get("/api/engagement/restart", headers=auth(token))).status_code == 404
    assert (await client.get("/api/engagement/desk/nope/referral",
                             headers=auth(token))).status_code == 404
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    r = await client.get("/api/engagement/restart", headers=auth(token))
    assert r.status_code == 200 and r.json() == []
    assert (await client.get("/api/engagement/desk/nope/referral",
                             headers=auth(token))).status_code == 404
