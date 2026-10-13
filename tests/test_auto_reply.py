"""The two reply switches (asked for 2026-10-13: "ensure the user can really set whether he wants
auto reply enabled or not").

* **Act on clear replies by itself** (on by default, today's behaviour): a confident "not now",
  "no thanks" or "unsubscribe" snoozes or stops the sequence. Off: it becomes `unclear`, the
  sequence pauses (nothing more is sent) and a person decides on the reply desk.
* **Send AI replies by itself** (off by default, reverses D22 only where switched on): the AI's
  answer to a confident interested buyer or question is emailed by the reply sweep, through the
  same send path an SDR uses, once per thread, and only when the draft passes every check.

Each switch is ALLOWED by a manager for the workspace and CHOSEN by each SDR for their own mailbox,
like the confidence bar (D23). Effective = allowed AND chosen. Real rows and real RFC 5322 bytes,
as the rest of the engine's tests (D21).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_replies import NOW, Mailbox, _classification, _mail, _sent_first, _sync
from tests.test_engagement_sequences import _enrollment


@pytest.fixture
def mailbox_double():
    """The reply suite's provider double: a Sent folder for sends, an inbox for arrivals."""
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


async def _switches(tid, *, allow_actions=True, allow_send=False, actions=True, send=False):
    from nexus.engagement.settings import read_settings, validate_update, with_settings
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import Tenant

    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        updated = validate_update(read_settings(tenant.email_settings), {
            "auto_actions_allowed": allow_actions, "auto_send_allowed": allow_send})
        tenant.email_settings = with_settings(tenant.email_settings, updated)
        for mailbox in await ts.list(MailboxConnection):
            mailbox.auto_actions = actions
            mailbox.auto_send_replies = send
        await ts.flush()


async def _sweep(tid):
    from nexus.engagement.replies.auto_reply import send_pending

    async with tenant_session(tid) as ts:
        return await send_pending(ts)


# ---- the settings -----------------------------------------------------------------------------------

def test_the_defaults_keep_todays_behaviour():
    from nexus.engagement.settings import DEFAULTS, read_settings

    assert DEFAULTS.auto_actions_allowed is True and DEFAULTS.auto_send_allowed is False
    # A JSON string "false" is truthy in Python; a stored value that is not a real boolean is the
    # default, never the opposite of what it says.
    stored = read_settings({"engagement": {"auto_actions_allowed": "false",
                                           "auto_send_allowed": "yes"}})
    assert stored.auto_actions_allowed is True and stored.auto_send_allowed is False


def test_a_switch_takes_only_a_boolean():
    from nexus.engagement.settings import DEFAULTS, validate_update

    with pytest.raises(ValueError, match="true or false"):
        validate_update(DEFAULTS, {"auto_send_allowed": "true"})
    assert validate_update(DEFAULTS, {"auto_send_allowed": True}).auto_send_allowed is True


def test_effective_is_allowed_and_chosen():
    from nexus.engagement.settings import DEFAULTS, EngagementSettings, reply_automation

    class Box:
        auto_actions = True
        auto_send_replies = True

    assert reply_automation(DEFAULTS, Box) == (True, False)  # sending is not allowed by default
    allowed = EngagementSettings(auto_send_allowed=True)
    assert reply_automation(allowed, Box) == (True, True)
    Box.auto_actions = False
    assert reply_automation(allowed, Box) == (False, True)
    closed = EngagementSettings(auto_actions_allowed=False, auto_send_allowed=False)
    Box.auto_actions = True
    assert reply_automation(closed, Box) == (False, False)


# ---- the API ----------------------------------------------------------------------------------------

async def _mailbox_for(client, slug):
    from nexus.engagement.mailboxes.service import upsert_connection

    token = await signup(client, slug=slug, email=f"sdr@{slug}co.com", company=slug.title())
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        row = await upsert_connection(
            ts, owner_user_id=me.user_id, provider="google", email=f"sdr@{slug}co.com",
            display_name="Sam", bundle={"access_token": "a", "refresh_token": "r",
                                        "expires_at": 1_900_000_000,
                                        "refreshed_at": 1_800_000_000},
            scopes=[], timezone="UTC")
        return token, me, row.id


async def test_a_manager_allows_and_the_sdr_chooses(client):
    token, me, mailbox_id = await _mailbox_for(client, "autoapi")
    settings = (await client.get("/api/engagement/settings", headers=auth(token))).json()
    assert settings["auto_actions_allowed"] is True and settings["auto_send_allowed"] is False

    row = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()[0]
    assert (row["auto_actions"], row["auto_send_replies"]) == (True, False)
    assert (row["acts_alone"], row["sends_alone"]) == (True, False)
    assert row["auto_send_allowed"] is False

    # Not allowed yet: the SDR's choice is refused with the reason, and nothing is stored.
    r = await client.patch(f"/api/engagement/mailboxes/{mailbox_id}", headers=auth(token),
                           json={"auto_send_replies": True})
    assert r.status_code == 422 and "allow" in r.json()["detail"]

    r = await client.put("/api/engagement/settings", headers=auth(token),
                         json={"auto_send_allowed": True})
    assert r.status_code == 200 and r.json()["auto_send_allowed"] is True
    r = await client.patch(f"/api/engagement/mailboxes/{mailbox_id}", headers=auth(token),
                           json={"auto_send_replies": True, "auto_actions": False})
    assert r.status_code == 200
    body = r.json()
    assert (body["auto_send_replies"], body["sends_alone"]) == (True, True)
    assert (body["auto_actions"], body["acts_alone"]) == (False, False)

    # Withdrawn for the workspace: the SDR's choice stays stored and stops taking effect.
    await client.put("/api/engagement/settings", headers=auth(token),
                     json={"auto_send_allowed": False})
    row = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()[0]
    assert (row["auto_send_replies"], row["sends_alone"]) == (True, False)


async def test_a_rep_cannot_change_the_workspace_switches(client):
    from nexus.core.security import create_access_token

    token, me, _ = await _mailbox_for(client, "autorep")
    rep = create_access_token(user_id="rep-user", tenant_id=me.tenant_id, role="rep")
    r = await client.put("/api/engagement/settings", headers=auth(rep),
                         json={"auto_send_allowed": True})
    assert r.status_code == 403


async def test_only_the_owner_switches_their_mailbox(client):
    from nexus.core.security import create_access_token

    token, me, mailbox_id = await _mailbox_for(client, "autoown")
    other = create_access_token(user_id="someone-else", tenant_id=me.tenant_id, role="manager")
    r = await client.patch(f"/api/engagement/mailboxes/{mailbox_id}", headers=auth(other),
                           json={"auto_actions": False})
    assert r.status_code == 403


# ---- acting on replies by itself ------------------------------------------------------------------

async def test_with_actions_off_an_unsubscribe_pauses_and_waits_for_a_person(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.suppression.service import active_block

    tid, enrollment, our_id = await _sent_first("autoact1", monkeypatch, mailbox_double)
    await _switches(tid, actions=False)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Please remove me from your list."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    # Nothing more is sent to them, and nobody but a person decides what happens next.
    assert (enrollment.status, enrollment.status_reason) == ("paused", "needs_decision")
    classification = await _classification(tid)
    assert classification.category == "unclear" and classification.status == "open"
    assert "switched off" in classification.reasoning
    async with tenant_session(tid) as ts:
        assert await active_block(ts, "jane0@acme.io") is None


async def test_the_workspace_can_switch_actions_off_for_every_mailbox(mailbox_double, monkeypatch):
    tid, enrollment, our_id = await _sent_first("autoact2", monkeypatch, mailbox_double)
    await _switches(tid, allow_actions=False, actions=True)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested, thanks."))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).category == "unclear"
    assert (await _enrollment(tid)).status == "paused"


async def test_with_actions_on_a_clear_no_is_still_acted_on(mailbox_double, monkeypatch):
    tid, enrollment, our_id = await _sent_first("autoact3", monkeypatch, mailbox_double)
    await _switches(tid, actions=True)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested, thanks."))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).category == "declined"
    assert (await _enrollment(tid)).status_reason == "declined"


# ---- sending AI replies by itself -----------------------------------------------------------------

async def test_off_by_default_nothing_is_sent_to_a_buyer(mailbox_double, monkeypatch):
    tid, enrollment, our_id = await _sent_first("autosend0", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).auto_reply is None
    assert await _sweep(tid) == {}
    assert len(mailbox_double.delivered) == 1  # only our first email


async def test_switched_on_the_ai_answers_an_interested_buyer_in_the_thread(
    mailbox_double, monkeypatch,
):
    from email import message_from_bytes

    from nexus.models.alerts import Alert
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, our_id = await _sent_first("autosend1", monkeypatch, mailbox_double)
    await _switches(tid, allow_send=True, send=True)
    mailbox_double.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).auto_reply == "pending"

    assert await _sweep(tid) == {"sent": 1}
    assert len(mailbox_double.delivered) == 2
    answer = message_from_bytes(mailbox_double.delivered[1])
    assert answer["To"].endswith("jane0@acme.io>") or answer["To"] == "jane0@acme.io"
    assert answer["Subject"].startswith("Re: ")
    classification = await _classification(tid)
    assert classification.auto_reply == "sent" and classification.status == "done"
    assert classification.responded_at is not None
    async with tenant_session(tid) as ts:
        out = await ts.list(EngagementMessage, EngagementMessage.kind == "response")
        alerts = await ts.list(Alert)
    assert len(out) == 1 and out[0].thread_id == enrollment.current_thread_id
    assert any(a.meta.get("category") == "reply_auto_sent" for a in alerts)
    # The sweep again sends nothing: once per reply.
    assert await _sweep(tid) == {}
    assert len(mailbox_double.delivered) == 2


async def test_switched_off_before_the_sweep_nothing_is_sent(mailbox_double, monkeypatch):
    tid, enrollment, our_id = await _sent_first("autosend2", monkeypatch, mailbox_double)
    await _switches(tid, allow_send=True, send=True)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    await _sync(tid, enrollment.mailbox_connection_id)
    await _switches(tid, allow_send=True, send=False)
    assert await _sweep(tid) == {"held": 1}
    classification = await _classification(tid)
    assert classification.auto_reply == "held" and classification.status == "open"
    assert "switched off" in classification.auto_reply_note
    assert len(mailbox_double.delivered) == 1


async def test_only_the_first_reply_in_a_thread_is_answered_automatically(
    mailbox_double, monkeypatch,
):
    from nexus.models.engagement import ReplyClassification

    tid, enrollment, our_id = await _sent_first("autosend3", monkeypatch, mailbox_double)
    await _switches(tid, allow_send=True, send=True)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    await _sync(tid, enrollment.mailbox_connection_id)
    await _sweep(tid)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Great, happy to chat. Who else should join?"),
                          when=NOW + timedelta(days=1))
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        rows = await ts.list(ReplyClassification)
    second = max(rows, key=lambda r: r.created_at)
    assert second.auto_reply is None and second.status == "open"
    assert await _sweep(tid) == {}
    assert len(mailbox_double.delivered) == 2


async def test_a_decline_or_an_unclear_reply_never_gets_an_automatic_email(
    mailbox_double, monkeypatch,
):
    tid, enrollment, our_id = await _sent_first("autosend4", monkeypatch, mailbox_double)
    await _switches(tid, allow_send=True, send=True)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested, thanks."))
    await _sync(tid, enrollment.mailbox_connection_id)
    assert (await _classification(tid)).auto_reply is None
    assert await _sweep(tid) == {}
    assert len(mailbox_double.delivered) == 1


def test_a_draft_that_names_a_time_is_held_for_the_rep():
    """The AI cannot see the rep's calendar, so an automatic reply never promises a time."""
    from nexus.engagement.replies.auto_reply import names_a_time

    assert names_a_time("Would Tuesday at 10:30 work?")
    assert names_a_time("How about 3pm on Thursday?")
    assert names_a_time("Free at 9 am tomorrow")
    assert not names_a_time("Which days suit you for a short call next week?")
    assert not names_a_time("We cut onboarding from 3 weeks to 2.")


def test_the_automatic_ask_never_offers_times():
    from nexus.agents.copy import AUTO_REPLY_CTA_RULE, email_rules

    rules = email_rules("response", automatic=True)
    assert AUTO_REPLY_CTA_RULE in rules and "two specific times" not in rules
    assert "two specific times" in email_rules("response")


async def test_the_sweep_is_scheduled_and_registered():
    import inspect

    from nexus.workers import scheduler
    from nexus.workers.tasks import HANDLERS, handle_send_auto_replies

    assert HANDLERS["send_auto_replies"] is handle_send_auto_replies
    assert "enqueue_send_auto_replies" in inspect.getsource(scheduler)
    assert (await handle_send_auto_replies({}))["tenants"] == 0
