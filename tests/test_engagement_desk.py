"""The reply desk: the three tabs, the suggested answer, the send, and the four decisions
(spec §9, §19, D3, D22).

Everything here runs on the same `Mailbox` provider double as reply ingestion, so a test starts
from a real sent email and a real reply rather than a hand-built classification row.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_replies import NOW, Mailbox, _mail, _sync
from tests.test_engagement_sequences import _enrollment, _launched, _run


@pytest.fixture
def mailbox_double():
    """The same provider double reply ingestion uses: a Sent folder, a Drafts folder, an inbox."""
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _replied(slug, monkeypatch, box, *, body="Sounds interesting, let's talk next week."):
    """A launched campaign whose first email went out and drew one reply, already classified."""
    from email import message_from_bytes

    from nexus.models.engagement import ReplyClassification

    tid, _campaign = await _launched(slug, monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    our_id = message_from_bytes(box.delivered[0])["Message-ID"]
    box.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id, body=body))
    enrollment = await _enrollment(tid)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        classification = await ts.first(ReplyClassification)
        return tid, classification.id, enrollment.mailbox_connection_id


async def _reload(tid, classification_id):
    from nexus.models.engagement import ReplyClassification

    async with tenant_session(tid) as ts:
        return await ts.get(ReplyClassification, classification_id)


async def _owner_of(tid):
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        mailbox = await ts.first(MailboxConnection)
        return mailbox.owner_user_id


# ---- pure rules ----------------------------------------------------------------------------------

def test_waiting_time_is_counted_in_business_hours_not_wall_clock():
    from nexus.engagement.desk.service import business_hours_between

    friday_evening = datetime(2026, 9, 18, 17, 0, tzinfo=UTC)
    # Saturday morning: 14 hours later by the clock, one of them inside a working day.
    assert business_hours_between(friday_evening, datetime(2026, 9, 19, 7, 0, tzinfo=UTC)) == 1
    # Monday 10:00 — one hour of Friday, none of the weekend, one of Monday.
    monday = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    assert business_hours_between(friday_evening, monday) == pytest.approx(2.0, abs=0.01)
    assert business_hours_between(monday, friday_evening) == 0
    # The clock runs in the reader's zone: 08:00 UTC is already 09:30 in Kolkata.
    assert business_hours_between(datetime(2026, 9, 21, 8, 0, tzinfo=UTC), monday,
                                  ZoneInfo("Asia/Kolkata")) == pytest.approx(2.0, abs=0.01)


async def test_a_decision_the_desk_does_not_have_is_refused(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import DeskError, decide

    tid, classification_id, _mailbox = await _replied("deskbad", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        with pytest.raises(DeskError):
            await decide(ts, classification, "ghost", user_id="u1")
        # Coming back on a date needs the date: a snooze with no day is a silent stop.
        with pytest.raises(DeskError):
            await decide(ts, classification, "reengage", user_id="u1")


async def _fetch(ts, classification_id):
    from nexus.models.engagement import ReplyClassification

    return await ts.get(ReplyClassification, classification_id)


# ---- the three tabs ------------------------------------------------------------------------------

async def test_the_queue_is_the_reps_own_mailboxes_and_a_manager_can_see_the_team(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import handled, needs_action

    tid, classification_id, _mailbox = await _replied("deskqueue", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        mine = await needs_action(ts, user_id=owner)
        assert [c.id for c in mine] == [classification_id]
        # A colleague works their own mailboxes and sees nothing of this one...
        assert await needs_action(ts, user_id="someone-else") == []
        # ...unless they are a manager asking for the team.
        team = await needs_action(ts, user_id="someone-else", team=True)
        assert [c.id for c in team] == [classification_id]
        # An open reply is not in Handled.
        assert await handled(ts, user_id=owner) == []


async def test_scheduled_lists_what_is_waiting_for_a_date(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import scheduled

    tid, _classification_id, _mailbox = await _replied(
        "desksched", monkeypatch, mailbox_double, body="Not now — try me in June please.")
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        waiting = await scheduled(ts, user_id=owner)
        assert [e.id for e in waiting] == [enrollment.id]


async def test_an_item_carries_the_whole_conversation_and_the_colleagues_it_paused(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import item
    from nexus.models.engagement import EngagementEnrollment

    tid, classification_id, _mailbox = await _replied("deskitem", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        # A colleague at the same account, paused by this reply (D3).
        colleague = EngagementEnrollment(
            campaign_id=(await ts.first(EngagementEnrollment)).campaign_id + "x",
            contact_id=classification.contact_id, account_id=classification.account_id,
            status="paused", status_reason="colleague_replied")
        ts.add(colleague)
        await ts.flush()
        detail = await item(ts, classification)
    assert [m.direction for m in detail.conversation] == ["out", "in"]
    assert detail.contact.email == "jane0@acme.io" and detail.account.name == "Acme Robotics"
    assert [e.id for e in detail.paused_colleagues] == [colleague.id]


# ---- the suggested answer ------------------------------------------------------------------------

async def test_the_suggested_answer_is_written_charged_and_kept_for_the_sdr(
    mailbox_double, monkeypatch,
):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.desk.service import draft_response
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    tid, classification_id, _mailbox = await _replied("deskdraft", monkeypatch, mailbox_double)
    # Metering is what is under test, so it is switched on after the campaign that set it up.
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        written = await draft_response(ts, classification, user_id=owner)
    assert written["subject"] and written["body"]
    stored = await _reload(tid, classification_id)
    assert stored.suggested_response.startswith(written["subject"])
    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == "ai.reply_draft")
    # Suggesting a reply is its own line on the bill, not the outbound draft's.
    assert len(events) == 1 and events[0].quantity == 1


async def test_sending_the_answer_threads_it_closes_the_item_and_records_the_conversation(
    mailbox_double, monkeypatch,
):
    from email import message_from_bytes

    from nexus.engagement.desk.service import send_response
    from nexus.engagement.ledger import consent
    from nexus.models.ledger import LedgerOutbox

    tid, classification_id, _mailbox = await _replied("desksend", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        classification = await _fetch(ts, classification_id)
        result = await send_response(ts, classification, user_id=owner,
                                     subject="Re: Quick question", body="Thursday at 10 works.")
    assert result.sent
    reply = message_from_bytes(mailbox_double.delivered[-1])
    assert reply["To"] == "jane0@acme.io" and reply["In-Reply-To"]
    stored = await _reload(tid, classification_id)
    assert stored.responded_at is not None and stored.status == "done"
    async with tenant_session(tid) as ts:
        events = await ts.list(LedgerOutbox,
                               LedgerOutbox.event_type == "response.sent")
    # The ledger keeps what the SDR sent AND the conversation it answered — the training pair.
    assert len(events) == 1
    assert events[0].payload["payload"]["body"] == "Thursday at 10 works."
    assert [m["direction"] for m in events[0].payload["payload"]["conversation"]] == ["out", "in"]


async def test_an_unclear_reply_stays_open_after_an_answer_because_it_still_needs_a_decision(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import send_response

    tid, classification_id, _mailbox = await _replied(
        "deskunclear", monkeypatch, mailbox_double,
        body="Not interested. Maybe reach out next year.")
    assert (await _reload(tid, classification_id)).category == "unclear"
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await send_response(ts, classification, user_id=owner, subject="Re: Quick question",
                            body="Understood — I will check back then.")
    stored = await _reload(tid, classification_id)
    assert stored.responded_at is not None and stored.status == "open"


async def test_saving_to_drafts_sends_nothing_and_charges_nothing(mailbox_double, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.desk.service import save_to_drafts
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    tid, classification_id, _mailbox = await _replied("deskkeep", monkeypatch, mailbox_double)
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    delivered = len(mailbox_double.delivered)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        draft_id = await save_to_drafts(ts, classification, subject="Re: Quick question",
                                        body="Let me come back to you.")
        sends = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == "outreach.email_send")
    assert draft_id == "draft-1" and len(mailbox_double.drafts) == 1
    assert len(mailbox_double.delivered) == delivered and sends == []


# ---- the four decisions --------------------------------------------------------------------------

async def test_coming_back_later_snoozes_every_live_enrollment_to_that_morning(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide

    tid, classification_id, _mailbox = await _replied("deskre", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "reengage", user_id=owner,
                     reengage_on=date(2027, 3, 4), note="Asked for March")
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    local = enrollment.snoozed_until.astimezone(ZoneInfo(enrollment.contact_timezone))
    assert (local.year, local.month, local.day, local.hour) == (2027, 3, 4, 9)
    stored = await _reload(tid, classification_id)
    assert stored.decision == "reengage" and stored.status == "done"


async def test_blocking_a_person_suppresses_the_address_and_stops_the_sequence(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.engagement.suppression.service import active_block

    tid, classification_id, _mailbox = await _replied("deskblock", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "block", user_id=owner)
        block = await active_block(ts, "jane0@acme.io")
    assert block is not None and block.reason == "manual"
    assert (await _enrollment(tid)).status == "stopped"


async def test_a_meeting_is_recorded_as_an_outcome_the_dashboards_can_see(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import decide
    from nexus.models.outcome import Outcome

    tid, classification_id, _mailbox = await _replied("deskmeet", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await decide(ts, classification, "meeting", user_id=owner)
        outcomes = await ts.list(Outcome)
    assert [o.stage for o in outcomes] == ["meeting"]
    assert outcomes[0].meta["source"] == "engagement_reply_desk"
    assert (await _enrollment(tid)).status == "stopped"


async def test_a_correction_is_the_label_the_ledger_trains_on(mailbox_double, monkeypatch):
    from nexus.engagement.desk.service import correct
    from nexus.engagement.ledger import consent
    from nexus.models.ledger import LedgerOutbox

    tid, classification_id, _mailbox = await _replied("deskfix", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        classification = await _fetch(ts, classification_id)
        await correct(ts, classification, "question", user_id=owner)
        events = await ts.list(LedgerOutbox,
                               LedgerOutbox.event_type == "reply.corrected")
    stored = await _reload(tid, classification_id)
    assert stored.corrected_category == "question" and stored.label_source == "sdr_corrected"
    assert events[0].payload["payload"]["ai_category"] == "interested"
    # Agreeing with the AI is a different label from correcting it, and worth as much.
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        await correct(ts, classification, "interested", user_id=owner)
    assert (await _reload(tid, classification_id)).label_source == "sdr_confirmed"


async def test_a_paused_colleague_resumes_only_because_a_person_said_so(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import DeskError, resume_colleague, stop_colleague
    from nexus.models.engagement import EngagementEnrollment

    tid, classification_id, _mailbox = await _replied("deskcol", monkeypatch, mailbox_double)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        first = await ts.first(EngagementEnrollment)
        colleague = EngagementEnrollment(
            campaign_id=first.campaign_id, contact_id=first.contact_id + "b",
            account_id=first.account_id, status="paused", status_reason="colleague_replied",
            mailbox_connection_id=first.mailbox_connection_id)
        other = EngagementEnrollment(
            campaign_id=first.campaign_id, contact_id=first.contact_id + "c",
            account_id=first.account_id, status="paused", status_reason="manual")
        ts.add(colleague)
        ts.add(other)
        await ts.flush()
        await resume_colleague(ts, colleague, user_id=owner)
        assert colleague.status == "active"
        # A pause somebody else chose is not the desk's to undo.
        with pytest.raises(DeskError):
            await resume_colleague(ts, other, user_id=owner)
        await stop_colleague(ts, other, user_id=owner)
        assert other.status == "stopped"


# ---- the reply-speed reminder --------------------------------------------------------------------

async def test_a_waiting_buyer_is_reminded_once_after_four_business_hours(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.desk.service import remind_unanswered
    from nexus.models.alerts import Alert

    tid, classification_id, _mailbox = await _replied("deskremind", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        classification.created_at = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)  # a Tuesday
        await ts.flush()
    async with tenant_session(tid) as ts:
        # Three hours later: still inside the promise.
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC)) == 0
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 14, 0, tzinfo=UTC)) == 1
        # A second sweep says nothing more about the same fact.
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 18, 0, tzinfo=UTC)) == 0
        alerts = await ts.list(Alert)
    assert sum(1 for a in alerts if a.meta.get("reminder")) == 1
    assert (await _reload(tid, classification_id)).reminded_at is not None


async def test_an_answered_reply_is_never_chased(mailbox_double, monkeypatch):
    from nexus.core.db import utcnow
    from nexus.engagement.desk.service import remind_unanswered

    tid, classification_id, _mailbox = await _replied("deskquiet", monkeypatch, mailbox_double)
    async with tenant_session(tid) as ts:
        classification = await _fetch(ts, classification_id)
        classification.created_at = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        classification.responded_at = utcnow()
        await ts.flush()
    async with tenant_session(tid) as ts:
        assert await remind_unanswered(ts, now=datetime(2026, 9, 22, 18, 0, tzinfo=UTC)) == 0


# ---- the API -------------------------------------------------------------------------------------

async def test_the_desk_is_not_there_while_the_engine_is_dark(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="deskdark", email="sam@deskdark.com", company="Dark")
    assert (await client.get("/api/engagement/desk", headers=auth(token))).status_code == 404


async def test_the_queue_the_item_and_a_decision_through_the_api(
    client, engine_on, mailbox_double, monkeypatch,
):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementMessage,
        EngagementThread,
        MailboxConnection,
        ReplyClassification,
    )
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="deskapi", email="sam@deskapi.com", company="Desk")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@deskapi.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        thread = EngagementThread(mailbox_connection_id=mailbox.id, provider_thread_id="t-1",
                                  base_subject="Quick question")
        ts.add(contact)
        ts.add(thread)
        await ts.flush()
        message = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                    contact_id=contact.id, direction="in", kind="reply",
                                    status="received", subject="Re: Quick question",
                                    body_text="Yes — how does Tuesday look?",
                                    from_addr="jane@acme.io", to_addrs=["sam@deskapi.com"],
                                    inbound_kind="human", received_at=NOW)
        ts.add(message)
        await ts.flush()
        ts.add(ReplyClassification(message_id=message.id, mailbox_connection_id=mailbox.id,
                                   contact_id=contact.id, account_id=account.id,
                                   category="question", confidence=0.9, status="open"))
        await ts.flush()

    queue = await client.get("/api/engagement/desk", headers=auth(token))
    assert queue.status_code == 200, queue.text
    rows = queue.json()
    assert len(rows) == 1 and rows[0]["category"] == "question"
    assert rows[0]["preview"] == "Yes — how does Tuesday look?"
    # The queue says who each reply is from without a request per row.
    assert (rows[0]["contact_name"], rows[0]["contact_email"], rows[0]["account_name"]) \
        == ("Jane Buyer", "jane@acme.io", "Acme Robotics")

    item = (await client.get(f"/api/engagement/desk/{rows[0]['id']}",
                             headers=auth(token))).json()
    assert item["body"] == "Yes — how does Tuesday look?"
    assert [m["direction"] for m in item["conversation"]] == ["in"]

    decided = await client.post(f"/api/engagement/desk/{rows[0]['id']}/decide",
                                headers=auth(token), json={"decision": "close"})
    assert decided.status_code == 204, decided.text
    assert (await client.get("/api/engagement/desk", headers=auth(token))).json() == []
    handled = (await client.get("/api/engagement/desk?tab=handled",
                                headers=auth(token))).json()
    assert [r["decision"] for r in handled] == ["close"]

    # A date is required for a re-engagement, and a decision nobody defined is refused.
    refused = await client.post(f"/api/engagement/desk/{rows[0]['id']}/decide",
                                headers=auth(token), json={"decision": "reengage"})
    assert refused.status_code == 409


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def test_a_reply_belongs_to_its_mailbox_owner_and_a_manager_can_hand_it_over(
    client, engine_on,
):
    from nexus.core.security import decode_access_token
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection, ReplyClassification
    from tests.conftest import principal_from_token

    owner_token = await signup(client, slug="deskpriv", email="boss@deskpriv.com", company="Priv")
    me = principal_from_token(owner_token)
    ann = await _member(client, owner_token, "ann@deskpriv.com", "rep")
    bob = await _member(client, owner_token, "bob@deskpriv.com", "rep")
    ann_id = (decode_access_token(ann) or {})["sub"]
    bob_id = (decode_access_token(bob) or {})["sub"]

    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=ann_id, provider="google",
                                    email="ann@deskpriv.com", status="connected")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        message = EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                    direction="in", kind="reply", status="received",
                                    subject="Re: Hello", body_text="Who is this?",
                                    from_addr="jane@acme.io", to_addrs=["ann@deskpriv.com"],
                                    inbound_kind="human", received_at=NOW)
        ts.add(message)
        await ts.flush()
        classification = ReplyClassification(message_id=message.id,
                                             mailbox_connection_id=mailbox.id,
                                             contact_id=contact.id, account_id=account.id,
                                             category="question", confidence=0.9, status="open")
        ts.add(classification)
        await ts.flush()
        classification_id = classification.id

    # Ann's mailbox, Ann's reply.
    assert [r["id"] for r in (await client.get("/api/engagement/desk",
                                               headers=auth(ann))).json()] == [classification_id]
    # Bob works his own mailboxes, and asking for the team does not make him a manager.
    assert (await client.get("/api/engagement/desk", headers=auth(bob))).json() == []
    assert (await client.get("/api/engagement/desk?team=true", headers=auth(bob))).json() == []
    assert (await client.get(f"/api/engagement/desk/{classification_id}",
                             headers=auth(bob))).status_code == 404
    assert (await client.post(f"/api/engagement/desk/{classification_id}/assign?user_id={bob_id}",
                              headers=auth(bob))).status_code == 403

    # The owner sees the team's queue and can hand the reply to Bob.
    team = (await client.get("/api/engagement/desk?team=true", headers=auth(owner_token))).json()
    assert [r["id"] for r in team] == [classification_id]
    assigned = await client.post(
        f"/api/engagement/desk/{classification_id}/assign?user_id={bob_id}",
        headers=auth(owner_token))
    assert assigned.status_code == 204, assigned.text
    team = (await client.get("/api/engagement/desk?team=true", headers=auth(owner_token))).json()
    assert team[0]["assigned_user_id"] == bob_id
