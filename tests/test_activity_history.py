"""Call and email history per person and per account (SDR round 2, 5.2, 2026-10-29).

`GET /contacts/{id}/activity` and `GET /accounts/{id}/activity`: every call logged and every email
that left or arrived, newest first. Real rows; the workspace's owner signs up and is the viewer
unless a test makes a rep.
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import auth, principal_from_token, signup, tenant_session

QUOTED_REPLY = (
    "Thanks Sam, Thursday works for a call.\n"
    "Send me the deck first.\n"
    "\n"
    "On Tue, Sep 22, 2026 at 9:00 AM Sam Rep <sam@hist.com> wrote:\n"
    "> Hi Jane, saw the Series B news.\n"
)


async def _history(client, slug: str):
    """Jane at Acme: one email out, her reply, a draft that never left, two calls (one placed live
    by a colleague, one logged on the owner's task), and an account-level call with no person."""
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallActivity, CallTask, PlacedCall
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
    async with tenant_session(me.tenant_id) as ts:
        colleague = User(email=f"kim@{slug}.com", full_name="Kim Caller", password_hash="x")
        ts.session.add(colleague)
        await ts.session.flush()
        ts.add(Membership(user_id=colleague.id, role="rep"))
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email=f"sam@{slug}.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        jane = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        bob = Contact(account_id=account.id, full_name="Bob Other", email="bob@acme.io")
        ts.add(jane)
        ts.add(bob)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4 push", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()
        enrollment = EngagementEnrollment(campaign_id=campaign.id, contact_id=jane.id,
                                          account_id=account.id, mailbox_connection_id=mailbox.id,
                                          status="active")
        ts.add(enrollment)
        await ts.flush()
        thread = EngagementThread(mailbox_connection_id=mailbox.id, provider_thread_id="t-jane",
                                  contact_id=jane.id, enrollment_id=enrollment.id,
                                  base_subject="series b")
        ts.add(thread)
        await ts.flush()
        sent = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                 enrollment_id=enrollment.id, contact_id=jane.id, direction="out",
                                 kind="step", status="sent", step_index=0,
                                 sent_at=now - timedelta(days=3), subject="series b",
                                 body_text="Hi Jane,\n\nSaw the Series B news.\n\nWorth a look?")
        reply = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                  enrollment_id=enrollment.id, contact_id=jane.id, direction="in",
                                  kind="reply", status="received",
                                  received_at=now - timedelta(days=2), subject="Re: series b",
                                  body_text=QUOTED_REPLY)
        draft = EngagementMessage(mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id,
                                  contact_id=jane.id, direction="out", kind="step",
                                  status="draft", step_index=1, subject="Re: series b",
                                  body_text="Never sent")
        for row in (sent, reply, draft):
            ts.add(row)
        await ts.flush()
        ts.add(ReplyClassification(message_id=reply.id, mailbox_connection_id=mailbox.id,
                                   contact_id=jane.id, account_id=account.id,
                                   category="interested", confidence=0.9, status="open"))
        task = CallTask(account_id=account.id, contact_id=jane.id, reason="Step 2",
                        owner_user_id=me.user_id)
        ts.add(task)
        await ts.flush()
        ts.add(PlacedCall(call_task_id=task.id, provider_call_id="CA-live", user_id=colleague.id,
                          placed_at=now - timedelta(days=1)))
        ts.add(CallActivity(call_task_id=task.id, account_id=account.id, contact_id=jane.id,
                            disposition="callback", notes="Call back Thursday 3pm",
                            duration_s=95, provider_call_id="CA-live",
                            occurred_at=now - timedelta(days=1)))
        ts.add(CallActivity(call_task_id=task.id, account_id=account.id, contact_id=jane.id,
                            disposition="voicemail", notes="", duration_s=None,
                            occurred_at=now - timedelta(hours=2)))
        ts.add(CallActivity(account_id=account.id, contact_id=None, disposition="gatekeeper",
                            notes="Reception took a message", occurred_at=now - timedelta(days=4)))
        await ts.flush()
        ids = {"jane": jane.id, "bob": bob.id, "account": account.id, "thread": thread.id,
               "campaign": campaign.id, "colleague": colleague.id, "me": me.user_id}
    return token, me, ids


async def test_a_contacts_history_is_calls_and_emails_newest_first(client):
    token, _me, ids = await _history(client, "hist")
    r = await client.get(f"/api/contacts/{ids['jane']}/activity", headers=auth(token))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(i["kind"], i["direction"] or i["disposition"]) for i in rows] == [
        ("call", "Voicemail"), ("call", "Callback"), ("email", "in"), ("email", "out")]

    voicemail, callback, reply, sent = rows
    # Who called: the person who placed it live, else the owner of the task it was logged on.
    assert (callback["by"], callback["category"], callback["notes"]) == \
        ("Kim Caller", "Follow-Up", "Call back Thursday 3pm")
    assert callback["duration_s"] == 95 and callback["placed_live"] is True
    assert voicemail["by"] == "Rep" and voicemail["placed_live"] is False
    assert voicemail["category"] == "Call Attempt" and voicemail["disposition_code"] == "voicemail"

    # A reply's first lines are hers, not our email quoted back underneath.
    assert reply["preview"] == "Thanks Sam, Thursday works for a call. Send me the deck first."
    assert reply["reply_category"] == "interested" and reply["status"] == "received"
    assert sent["campaign_name"] == "Q4 push" and sent["campaign_id"] == ids["campaign"]
    assert sent["preview"].startswith("Hi Jane, Saw the Series B news.")
    assert sent["thread_link"] == f"/engagement/inbox?thread={ids['thread']}"
    assert all(i["contact_name"] == "Jane Buyer" for i in rows)
    assert "Never sent" not in {i["preview"] for i in rows}, "a draft is not history"


async def test_an_accounts_history_covers_everyone_there_and_calls_with_no_person(client):
    token, _me, ids = await _history(client, "histacct")
    rows = (await client.get(f"/api/accounts/{ids['account']}/activity",
                             headers=auth(token))).json()
    assert len(rows) == 5
    assert rows[-1]["disposition"] == "Gatekeeper" and rows[-1]["contact_id"] is None
    assert rows[-1]["by"] == "", "nobody is named for a call nobody can be tied to"


async def test_another_workspaces_ids_and_a_removed_contact_are_not_found(client):
    from nexus.core.db import utcnow
    from nexus.models.account import Contact

    token, me, ids = await _history(client, "histone")
    stranger = await signup(client, slug="histtwo", email="eve@histtwo.com", company="Two")
    for path in (f"/api/contacts/{ids['jane']}/activity", f"/api/accounts/{ids['account']}/activity"):
        assert (await client.get(path, headers=auth(stranger))).status_code == 404, path
    async with tenant_session(me.tenant_id) as ts:
        (await ts.get(Contact, ids["bob"])).deleted_at = utcnow()
    assert (await client.get(f"/api/contacts/{ids['bob']}/activity",
                             headers=auth(token))).status_code == 404


async def test_a_rep_sees_a_colleagues_email_but_gets_no_link_into_their_mailbox(client):
    from nexus.core.security import create_access_token

    _token, me, ids = await _history(client, "histrep")
    rep = create_access_token(user_id=ids["colleague"], tenant_id=me.tenant_id, role="rep")
    rows = (await client.get(f"/api/contacts/{ids['jane']}/activity", headers=auth(rep))).json()
    emails = [i for i in rows if i["kind"] == "email"]
    assert len(emails) == 2 and all(i["thread_link"] == "" for i in emails)


async def test_a_caller_who_has_left_the_workspace_is_a_former_member(client):
    from nexus.models.identity import Membership

    token, me, ids = await _history(client, "histgone")
    async with tenant_session(me.tenant_id) as ts:
        for m in await ts.list(Membership, Membership.user_id == ids["colleague"]):
            await ts.delete(m)
    rows = (await client.get(f"/api/contacts/{ids['jane']}/activity", headers=auth(token))).json()
    assert rows[1]["by"] == "Former member"
