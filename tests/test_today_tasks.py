"""Every due step is a task (SDR round 2, W3.2, 2026-10-29).

The Inbox is the one daily list, built from `GET /engagement/today`. Each item says which section it
belongs to, whether it is due today or overdue, the one action that clears it, and the person and
account behind it, so a row can open them in place. Built from real rows with fixed offsets from
now; the mailbox is on UTC, so "today" is the UTC day.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _day(client, slug: str):
    """A rep's day: a reply from two days ago, calls due today and overdue, a dated callback and an
    undated power-list call, an opening email waiting and a follow-up held for review."""
    from nexus.core.db import utcnow
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        EngagementStep,
        EngagementThread,
        MailboxConnection,
        ReplyClassification,
    )

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    now = utcnow()
    ids: dict[str, str] = {}
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email=f"sam@{slug}.com", status="connected", timezone="UTC")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = {name: Contact(account_id=account.id, full_name=name,
                                email=f"{name.lower()}@acme.io")
                  for name in ("Rae", "Cal", "Old", "Dot", "Pow", "Ola", "Fay")}
        for p in people.values():
            ts.add(p)
        await ts.flush()
        campaign = EngagementCampaign(name="Q4", owner_user_id=me.user_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(campaign)
        await ts.flush()
        for index, channel in enumerate(("email", "email", "call")):
            ts.add(EngagementStep(campaign_id=campaign.id, step_index=index, channel=channel))

        def enrol(name, **fields):
            row = EngagementEnrollment(campaign_id=campaign.id, contact_id=people[name].id,
                                       account_id=account.id, mailbox_connection_id=mailbox.id,
                                       **fields)
            ts.add(row)
            return row

        thread = EngagementThread(mailbox_connection_id=mailbox.id, provider_thread_id="t-rae",
                                  contact_id=people["Rae"].id, base_subject="Hello")
        ts.add(thread)
        await ts.flush()
        inbound = EngagementMessage(mailbox_connection_id=mailbox.id, thread_id=thread.id,
                                    contact_id=people["Rae"].id, direction="in", kind="reply",
                                    status="received", received_at=now - timedelta(days=2),
                                    subject="Re: Hello", body_text="Yes, tell me more")
        ts.add(inbound)
        await ts.flush()
        reply = ReplyClassification(message_id=inbound.id, mailbox_connection_id=mailbox.id,
                                    contact_id=people["Rae"].id, account_id=account.id,
                                    category="interested", confidence=0.9, status="open")
        ts.add(reply)

        today_call = enrol("Cal", status="active")
        old_call = enrol("Old", status="active")
        enrol("Ola", status="awaiting_review")
        held = enrol("Fay", status="awaiting_review", current_step_index=1,
                     started_at=now - timedelta(days=4), next_action_at=now - timedelta(days=2))
        await ts.flush()
        draft = EngagementMessage(mailbox_connection_id=mailbox.id, enrollment_id=held.id,
                                  contact_id=people["Fay"].id, direction="out", kind="step",
                                  status="draft", step_index=1, subject="Re: Hello",
                                  body_text="Following up", quality_problems=["no greeting"])
        ts.add(draft)
        calls = {
            "today": CallTask(account_id=account.id, contact_id=people["Cal"].id,
                              reason="Step 3 of Q4", owner_user_id=me.user_id, due_at=now,
                              engagement_enrollment_id=today_call.id),
            "overdue": CallTask(account_id=account.id, contact_id=people["Old"].id,
                                reason="Step 3 of Q4", owner_user_id=me.user_id,
                                due_at=now - timedelta(days=2),
                                engagement_enrollment_id=old_call.id),
            "callback": CallTask(account_id=account.id, contact_id=people["Dot"].id,
                                 reason="Asked for a call back", owner_user_id=me.user_id,
                                 due_at=now),
            "undated": CallTask(account_id=account.id, contact_id=people["Pow"].id,
                                reason="Power list", owner_user_id=me.user_id, due_at=None),
        }
        for call in calls.values():
            ts.add(call)
        await ts.flush()
        ids.update({f"call_{k}": v.id for k, v in calls.items()})
        ids.update(reply=reply.id, draft=draft.id, campaign=campaign.id, account=account.id,
                   fay=people["Fay"].id, cal=people["Cal"].id)
    return token, ids


async def test_every_due_step_is_a_task_with_its_section_due_and_action(client, engine_on):
    token, ids = await _day(client, "tasksday")
    r = await client.get("/api/engagement/today", headers=auth(token))
    assert r.status_code == 200, r.text
    items = r.json()
    # The order is still "most urgent first" by kind, for the dashboard and the MCP overview.
    kinds = [i["kind"] for i in items]
    assert kinds == ["reply", "call", "call", "call", "followup", "review"]

    reply = items[0]
    assert (reply["section"], reply["due"], reply["action"]) == ("replies", "overdue", "Reply")
    assert reply["ref_id"] == ids["reply"] and reply["contact_name"] == "Rae"
    assert reply["account_name"] == "Acme" and reply["account_id"] == ids["account"]

    calls = {i["ref_id"]: i for i in items if i["kind"] == "call"}
    assert set(calls) == {ids["call_today"], ids["call_overdue"], ids["call_callback"]}, \
        "a dated callback is today's task; an undated power-list call is the Calls page's"
    assert calls[ids["call_overdue"]]["due"] == "overdue"
    assert calls[ids["call_today"]]["due"] == "today"
    today_call = calls[ids["call_today"]]
    assert today_call["section"] == "calls" and today_call["action"] == "Call"
    assert today_call["link"] == f"/calls?task={ids['call_today']}"
    assert today_call["contact_id"] == ids["cal"]

    followup = next(i for i in items if i["kind"] == "followup")
    assert (followup["section"], followup["due"], followup["action"]) == \
        ("emails", "overdue", "Review and send")
    assert followup["ref_id"] == ids["draft"] and followup["contact_id"] == ids["fay"]
    assert "failed a check: no greeting" in followup["detail"]
    assert followup["link"] == f"/engagement/campaigns/{ids['campaign']}?tab=review"

    review = next(i for i in items if i["kind"] == "review")
    # The held follow-up is not one more "opening email": only the person never started is.
    assert review["count"] == 1 and review["title"] == "Review 1 opening email in Q4"
    assert review["section"] == "emails" and review["ref_id"] == ids["campaign"]


async def test_a_colleagues_calls_and_campaigns_are_not_on_my_list(client, engine_on):
    from nexus.models.calling import CallTask
    from nexus.models.identity import User

    token, ids = await _day(client, "tasksmine")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        other = User(email="other@tasksmine.com", full_name="Other", password_hash="x")
        ts.session.add(other)
        await ts.session.flush()
        call = await ts.get(CallTask, ids["call_callback"])
        call.owner_user_id = other.id
    items = (await client.get("/api/engagement/today", headers=auth(token))).json()
    assert ids["call_callback"] not in {i["ref_id"] for i in items}
