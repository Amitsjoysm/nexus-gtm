"""Reporting on the engagement engine (spec §11, §19): campaign results, per-step reply rate,
response times, Outcome rows for the dashboards, the Today plan, and mailbox health.

Campaign tests run the real flow (launch, send through the provider double, a real reply ingested);
the time-sensitive ones build rows with fixed timestamps, because business hours depend on the day.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_replies import Mailbox, _mail, _sync
from tests.test_engagement_sequences import _enrollment, _launched, _run


@pytest.fixture
def mailbox_double():
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _report(tid, campaign_id):
    from nexus.engagement.reports.service import campaign_report
    from nexus.models.engagement import EngagementCampaign

    async with tenant_session(tid) as ts:
        return await campaign_report(ts, await ts.get(EngagementCampaign, campaign_id))


async def _reply(box, our_message: bytes, body: str, *, minutes: int = 5):
    arrived = datetime.now(UTC) + timedelta(minutes=minutes)
    box.arrive(_mail(sender="jane0@acme.io", in_reply_to=message_from_bytes(our_message)["Message-ID"],
                     body=body, when=arrived), when=arrived)


# ---- pure rules ----------------------------------------------------------------------------------

def test_a_bounce_rate_warns_above_three_percent_once_there_is_enough_to_judge():
    from nexus.engagement.reports.health import assess

    assert assess(40, 2).warning, "5% of 40 sends is a real problem"
    assert not assess(40, 1).warning, "2.5% is inside the line"
    # One bounce in five sends is 20%, and means nothing yet.
    few = assess(5, 1)
    assert few.bounce_rate_7d == 0.2 and not few.warning
    assert assess(0, 0).bounce_rate_7d == 0.0


# ---- a campaign's results ------------------------------------------------------------------------

async def test_the_funnel_counts_people_and_credits_the_step_that_drew_the_reply(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.engagement import ReplyClassification

    tid, campaign_id = await _launched("repfunnel", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    assert len(mailbox_double.delivered) == 2
    # The reply comes after the SECOND email, so the second step earned it.
    await _reply(mailbox_double, mailbox_double.delivered[1], "Sounds interesting, let's talk.")
    await _reply(mailbox_double, mailbox_double.delivered[1], "Also, Thursday works.", minutes=9)
    await _sync(tid, enrollment.mailbox_connection_id)

    report = await _report(tid, campaign_id)
    assert (report.contacts, report.sent, report.replied, report.positive) == (1, 1, 1, 1)
    steps = {s.step_index: s for s in report.steps}
    assert (steps[0].sent, steps[0].replies) == (1, 0)
    assert (steps[1].sent, steps[1].replies) == (1, 1)
    assert steps[1].reply_rate == 1.0
    # Two replies from one person are one reply to the campaign, and two readings.
    assert sum(report.categories.values()) == 2

    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        await decide(ts, classification, "meeting", user_id="u1")
    assert (await _report(tid, campaign_id)).meetings == 1


async def test_sends_replies_and_meetings_reach_the_outcome_funnel_once_each(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.engagement import ReplyClassification
    from nexus.models.outcome import Outcome

    tid, campaign_id = await _launched("repoutcome", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    await _reply(mailbox_double, mailbox_double.delivered[0], "Interested, let's talk.")
    await _reply(mailbox_double, mailbox_double.delivered[0], "Following up on my reply.", minutes=9)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        await decide(ts, classification, "meeting", user_id="u1")
        outcomes = await ts.list(Outcome)
    stages = sorted(o.stage for o in outcomes)
    # One sent (one email left), one replied (however many times they wrote), one meeting.
    assert stages == ["meeting", "replied", "sent"]
    for o in outcomes:
        assert o.meta["engagement_campaign_id"] == campaign_id
        assert o.meta["enrollment_id"] == enrollment.id
        # The old campaigns table's key is left alone (spec §13).
        assert o.campaign_id is None


async def test_the_outcomes_api_takes_an_engagement_campaign(client, engine_on, monkeypatch):
    from nexus.models.engagement import EngagementCampaign, MailboxConnection

    token = await signup(client, slug="repapi", email="sam@repapi.com", company="R")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@repapi.com", status="connected")
        ts.add(mailbox)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()
        campaign_id = campaign.id
    ok = await client.post("/api/outcomes", headers=auth(token), json={
        "stage": "won", "engagement_campaign_id": campaign_id})
    assert ok.status_code == 201, ok.text
    missing = await client.post("/api/outcomes", headers=auth(token), json={
        "stage": "won", "engagement_campaign_id": "nope"})
    assert missing.status_code == 404
    report = await client.get(f"/api/engagement/reports/campaigns/{campaign_id}",
                              headers=auth(token))
    assert report.status_code == 200 and report.json()["contacts"] == 0


# ---- how fast the team answers -------------------------------------------------------------------

async def _conversation(ts, mailbox, contact, *, received: datetime, answered: datetime | None,
                        category: str = "interested"):
    from nexus.models.engagement import EngagementMessage, EngagementThread, ReplyClassification

    thread = EngagementThread(mailbox_connection_id=mailbox.id,
                              provider_thread_id=f"t-{contact.id}-{received.isoformat()}",
                              contact_id=contact.id, base_subject="Hello")
    ts.add(thread)
    await ts.flush()
    inbound = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                contact_id=contact.id, direction="in", kind="reply",
                                status="received", inbound_kind="human", received_at=received,
                                subject="Re: Hello", body_text="Yes please")
    ts.add(inbound)
    await ts.flush()
    ts.add(ReplyClassification(message_id=inbound.id, mailbox_connection_id=mailbox.id,
                               contact_id=contact.id, account_id=contact.account_id,
                               category=category, confidence=0.9,
                               status="done" if answered else "open"))
    if answered:
        ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                 contact_id=contact.id, direction="out", kind="response",
                                 status="sent", sent_at=answered, subject="Re: Hello",
                                 body_text="Great"))
    await ts.flush()


async def test_response_time_is_measured_in_the_mailboxs_business_hours(client, engine_on):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection

    token = await signup(client, slug="repspeed", email="sam@repspeed.com", company="S")
    me = principal_from_token(token)
    tuesday_9 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)          # London is UTC+1 in September
    friday_17 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)         # 17:00 London
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@repspeed.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = [Contact(account_id=account.id, full_name=f"P{i}", email=f"p{i}@acme.io")
                  for i in range(4)]
        for p in people:
            ts.add(p)
        await ts.flush()
        # Answered two hours later, the same working morning.
        await _conversation(ts, mailbox, people[0], received=tuesday_9,
                            answered=tuesday_9 + timedelta(hours=2))
        # Friday 17:00 to Monday 10:00: one hour Friday, one Monday, not 65 by the clock.
        await _conversation(ts, mailbox, people[1], received=friday_17,
                            answered=friday_17 + timedelta(days=2, hours=17))
        # Still waiting, and a decline that never needed an answer.
        await _conversation(ts, mailbox, people[2], received=tuesday_9, answered=None)
        await _conversation(ts, mailbox, people[3], received=tuesday_9, answered=None,
                            category="declined")

    from nexus.engagement.reports.service import response_times

    async with tenant_session(me.tenant_id) as ts:
        rows = await response_times(ts, user_id=me.user_id, team=False,
                                    now=datetime(2026, 9, 22, tzinfo=UTC))
    assert len(rows) == 1
    row = rows[0]
    assert (row.answered, row.waiting) == (2, 1)
    assert row.median_hours == pytest.approx(2.0, abs=0.01)
    r = await client.get("/api/engagement/reports/response-times", headers=auth(token),
                         params={"days": 90})
    assert r.status_code == 200 and r.json()[0]["name"]


# ---- today ---------------------------------------------------------------------------------------

async def test_today_puts_waiting_buyers_first_and_returning_people_last(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    token = await signup(client, slug="reptoday", email="sam@reptoday.com", company="T")
    me = principal_from_token(token)
    now = utcnow()
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@reptoday.com", status="connected", timezone="UTC")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = [Contact(account_id=account.id, full_name=f"P{i}", email=f"p{i}@acme.io")
                  for i in range(6)]
        for p in people:
            ts.add(p)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()

        def enroll(person, **fields):
            row = EngagementEnrollment(campaign_id=campaign.id, contact_id=person.id,
                                       account_id=account.id, mailbox_connection_id=mailbox.id,
                                       **fields)
            ts.add(row)
            return row

        enroll(people[0], status="snoozed", status_reason="later",
               snoozed_until=now.replace(hour=23, minute=0, second=0, microsecond=0))
        enroll(people[1], status="paused", status_reason="colleague_replied")
        enroll(people[2], status="awaiting_review")
        calling = enroll(people[3], status="active")
        await ts.flush()
        ts.add(CallTask(account_id=account.id, contact_id=people[3].id, reason="Step 3 of Q4",
                        owner_user_id=me.user_id, due_at=now,
                        engagement_enrollment_id=calling.id))
        await ts.flush()
        await _conversation(ts, mailbox, people[4], received=now - timedelta(hours=3),
                            answered=None, category="unclear")
        await _conversation(ts, mailbox, people[5], received=now - timedelta(hours=1),
                            answered=None, category="interested")

    r = await client.get("/api/engagement/today", headers=auth(token))
    assert r.status_code == 200, r.text
    kinds = [i["kind"] for i in r.json()]
    assert kinds == ["reply", "decide", "colleagues", "call", "review", "returning"]
    first = r.json()[0]
    assert first["title"] == "Answer P5 at Acme" and first["link"].startswith("/engagement/replies?reply=")


# ---- mailbox health, where the SDR already looks -------------------------------------------------

async def test_my_mailboxes_reports_this_weeks_bounce_rate(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection

    token = await signup(client, slug="rephealth", email="sam@rephealth.com", company="H")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@rephealth.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        for i in range(25):
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, contact_id=contact.id, direction="out",
                kind="oneoff", status="bounced" if i < 2 else "sent",
                sent_at=utcnow() - timedelta(days=1), subject="Hi", body_text="x"))
        # Older than a week: not this week's problem.
        ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                 direction="out", kind="oneoff", status="bounced",
                                 sent_at=utcnow() - timedelta(days=9), subject="Hi", body_text="x"))
        await ts.flush()
    row = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()[0]
    assert (row["sent_7d"], row["bounced_7d"]) == (25, 2)
    assert row["bounce_rate_7d"] == 0.08 and "bounced" in row["health_warning"]


async def test_the_reports_are_dark_with_the_engine(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="repdark", email="sam@repdark.com", company="D")
    for path in ("/api/engagement/today", "/api/engagement/reports/response-times"):
        assert (await client.get(path, headers=auth(token))).status_code == 404
