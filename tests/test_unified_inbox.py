"""The unified inbox (SDR round 2, 6.4, 2026-10-29): every engagement email sent and received, one
conversation per row, with filters and a reading pane. Read-only: answering is the reply desk's.

The owner signs up (a manager) with one mailbox; a colleague, a rep, has another. Real rows.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _mail(client, slug: str):
    from nexus.core.db import utcnow
    from nexus.core.security import create_access_token
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        MailboxConnection,
        ReplyClassification,
    )
    from nexus.models.identity import Membership, User

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    now = utcnow()
    ids: dict[str, str] = {}
    async with tenant_session(me.tenant_id) as ts:
        kim = User(email=f"kim@{slug}.com", full_name="Kim Rep", password_hash="x")
        ts.session.add(kim)
        await ts.session.flush()
        ts.add(Membership(user_id=kim.id, role="rep"))
        mine = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                 email=f"sam@{slug}.com", status="connected")
        theirs = MailboxConnection(owner_user_id=kim.id, provider="microsoft",
                                   email=f"kim@{slug}.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        for row in (mine, theirs, account):
            ts.add(row)
        await ts.flush()
        people = {n: Contact(account_id=account.id, full_name=f"{n} Buyer", email=f"{n}@acme.io")
                  for n in ("ana", "ben", "cy", "dee", "eli")}
        for p in people.values():
            ts.add(p)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4 push", owner_user_id=me.user_id,
                                      mailbox_connection_id=mine.id, status="active")
        ts.add(campaign)
        await ts.flush()
        enrollment = EngagementEnrollment(campaign_id=campaign.id, contact_id=people["ana"].id,
                                          account_id=account.id, mailbox_connection_id=mine.id,
                                          status="paused")
        ts.add(enrollment)
        await ts.flush()

        async def conversation(key, mailbox, person, hours_ago, *, reply=None, enrolled=None,
                               status="sent"):
            last = now - timedelta(hours=hours_ago)
            thread = EngagementThread(mailbox_connection_id=mailbox.id,
                                      provider_thread_id=f"t-{key}", contact_id=person.id,
                                      enrollment_id=enrolled, base_subject=f"about {key}",
                                      last_message_at=last)
            ts.add(thread)
            await ts.flush()
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, thread_id=thread.id, contact_id=person.id,
                enrollment_id=enrolled, direction="out", kind="step", status=status,
                sent_at=last - timedelta(hours=1) if status == "sent" else None,
                subject=f"about {key}", body_text=f"Hi {person.full_name},\n\nOpening for {key}."))
            if reply is not None:
                inbound = EngagementMessage(
                    mailbox_connection_id=mailbox.id, thread_id=thread.id, contact_id=person.id,
                    enrollment_id=enrolled, direction="in", kind="reply", status="received",
                    received_at=last, subject=f"Re: about {key}",
                    body_text=f"Reply on {key}.\n\nOn Mon, someone <x@y.z> wrote:\n> Opening")
                ts.add(inbound)
                await ts.flush()
                reading = ReplyClassification(
                    message_id=inbound.id, mailbox_connection_id=mailbox.id,
                    contact_id=person.id, account_id=account.id, category="interested",
                    confidence=0.9, status=reply)
                ts.add(reading)
                await ts.flush()
                ids[f"{key}_reply"] = reading.id
            await ts.flush()
            ids[key] = thread.id

        await conversation("waiting", mine, people["ana"], 1, reply="open", enrolled=enrollment.id)
        await conversation("quiet", mine, people["ben"], 3)
        await conversation("answered", mine, people["cy"], 5, reply="done")
        await conversation("kims", theirs, people["dee"], 2)
        await conversation("queued", mine, people["eli"], 0, status="queued")
        ids.update(mine=mine.id, theirs=theirs.id, campaign=campaign.id, kim=kim.id)
    rep = create_access_token(user_id=ids["kim"], tenant_id=me.tenant_id, role="rep")
    return token, rep, ids


async def _list(client, token, **query):
    r = await client.get("/api/engagement/inbox", headers=auth(token),
                         params={k: str(v).lower() if isinstance(v, bool) else v
                                 for k, v in query.items()})
    assert r.status_code == 200, r.text
    return r.json()


async def test_my_inbox_is_every_conversation_on_my_mailboxes_newest_first(client, engine_on):
    token, _rep, ids = await _mail(client, "uniq")
    body = await _list(client, token)
    rows = body["threads"]
    assert [r["thread_id"] for r in rows] == [ids["waiting"], ids["quiet"], ids["answered"]], \
        "newest first; a colleague's mailbox and a thread with nothing sent yet are not here"
    waiting = rows[0]
    assert (waiting["replied"], waiting["unread"], waiting["message_count"]) == (True, True, 2)
    assert waiting["last_direction"] == "in" and waiting["snippet"] == "Reply on waiting."
    assert waiting["campaign_name"] == "Q4 push" and waiting["mailbox_email"] == "sam@uniq.com"
    assert waiting["contact_name"] == "ana Buyer" and waiting["account_name"] == "Acme"
    assert (rows[1]["replied"], rows[1]["unread"]) == (False, False)
    assert (rows[2]["replied"], rows[2]["unread"]) == (True, False)
    assert [m["label"] for m in body["mailboxes"]] == ["sam@uniq.com"] and body["team"] is False


async def test_the_filters_narrow_the_list(client, engine_on):
    token, _rep, ids = await _mail(client, "unifilter")

    def thread_ids(body):
        return [r["thread_id"] for r in body["threads"]]

    assert thread_ids(await _list(client, token, replied=True)) == [ids["waiting"], ids["answered"]]
    assert thread_ids(await _list(client, token, unread=True)) == [ids["waiting"]]
    assert thread_ids(await _list(client, token, campaign_id=ids["campaign"])) == [ids["waiting"]]
    # A mailbox outside the view is not a way into it.
    assert thread_ids(await _list(client, token, mailbox_id=ids["theirs"])) == []
    team = await _list(client, token, team=True, mailbox_id=ids["theirs"])
    assert thread_ids(team) == [ids["kims"]] and team["team"] is True
    assert {m["label"] for m in team["mailboxes"]} == {"sam@unifilter.com", "kim@unifilter.com"}


async def test_a_rep_reads_only_their_own_mail_and_cannot_ask_for_the_team(client, engine_on):
    token, rep, ids = await _mail(client, "unirep")
    body = await _list(client, rep, team=True)
    assert [r["thread_id"] for r in body["threads"]] == [ids["kims"]] and body["team"] is False
    assert (await client.get(f"/api/engagement/inbox/threads/{ids['waiting']}",
                             headers=auth(rep))).status_code == 404
    # A manager opens any thread on the team's mailboxes, as the link from a history does.
    assert (await client.get(f"/api/engagement/inbox/threads/{ids['kims']}",
                             headers=auth(token))).status_code == 200


async def test_a_thread_reads_oldest_first_and_answers_on_the_reply_desk(client, engine_on):
    token, _rep, ids = await _mail(client, "unithread")
    r = await client.get(f"/api/engagement/inbox/threads/{ids['waiting']}", headers=auth(token))
    assert r.status_code == 200, r.text
    view = r.json()
    assert [m["direction"] for m in view["messages"]] == ["out", "in"]
    assert view["messages"][1]["body"] == "Reply on waiting.", "the quoted history is left out"
    assert view["messages"][1]["category"] == "interested"
    assert view["reply_open"] is True
    assert view["reply_link"] == f"/engagement/mail?folder=needs_reply&reply={ids['waiting_reply']}"
    done = (await client.get(f"/api/engagement/inbox/threads/{ids['answered']}",
                             headers=auth(token))).json()
    assert done["reply_link"] == f"/engagement/mail?folder=handled&reply={ids['answered_reply']}"
    quiet = (await client.get(f"/api/engagement/inbox/threads/{ids['quiet']}",
                              headers=auth(token))).json()
    assert quiet["reply_id"] is None and quiet["reply_link"] == ""


async def test_another_workspace_cannot_open_a_thread(client, engine_on):
    _token, _rep, ids = await _mail(client, "unione")
    stranger = await signup(client, slug="unitwo", email="eve@unitwo.com", company="Two")
    assert (await client.get(f"/api/engagement/inbox/threads/{ids['waiting']}",
                             headers=auth(stranger))).status_code == 404
    assert (await _list(client, stranger))["threads"] == []


def test_the_inbox_sends_nothing():
    """Answering goes through the reply desk's one send path; this router only reads."""
    from nexus.api.routers.engagement_inbox import router

    assert {m for route in router.routes for m in route.methods} == {"GET"}


async def test_the_inbox_is_dark_with_the_engine(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="unidark", email="sam@unidark.com", company="D")
    assert (await client.get("/api/engagement/inbox", headers=auth(token))).status_code == 404


# ---- Mail holds the reply desk (2026-11-04) -------------------------------------------------------

def test_a_reply_splits_from_the_history_it_quotes():
    from nexus.engagement.replies.parse import split_quoted, strip_quoted

    text = "Sounds good, Tuesday works.\n\nOn Mon, Sam <sam@ours.io> wrote:\n> Worth a look?\n> Sam"
    reply, quoted = split_quoted(text)
    assert reply == "Sounds good, Tuesday works." == strip_quoted(text)
    assert quoted.startswith("On Mon, Sam") and "> Worth a look?" in quoted
    assert split_quoted("Just this.") == ("Just this.", "")
    assert split_quoted("") == ("", "")
    inline = split_quoted("Yes please.\n> earlier line\nThanks")
    assert inline == ("Yes please.\nThanks", "> earlier line")


async def test_the_quoted_history_is_kept_folded_not_thrown_away(client, engine_on):
    token, _rep, ids = await _mail(client, "uniquote")
    view = (await client.get(f"/api/engagement/inbox/threads/{ids['waiting']}",
                             headers=auth(token))).json()
    reply = view["messages"][1]
    assert reply["body"] == "Reply on waiting."
    assert reply["quoted"].startswith("On Mon, someone") and "> Opening" in reply["quoted"]
    assert view["messages"][0]["quoted"] == "", "what we sent is shown whole"
    desk = (await client.get(f"/api/engagement/desk/{ids['waiting_reply']}",
                             headers=auth(token))).json()
    inbound = [m for m in desk["conversation"] if m["direction"] == "in"][0]
    assert inbound["body"] == "Reply on waiting." and "> Opening" in inbound["quoted"]


async def test_the_sent_folder_is_what_nothing_came_back_on(client, engine_on):
    token, _rep, ids = await _mail(client, "unisent")
    assert [r["thread_id"] for r in (await _list(client, token, unanswered=True))["threads"]] == \
        [ids["quiet"]]


async def test_the_reply_list_previews_what_they_wrote_not_the_quoted_trail(client, engine_on):
    token, _rep, ids = await _mail(client, "uniprev")
    rows = (await client.get("/api/engagement/desk", headers=auth(token),
                             params={"status": "needs_action"})).json()
    [mine] = [r for r in rows if r["id"] == ids["waiting_reply"]]
    assert mine["preview"] == "Reply on waiting."
