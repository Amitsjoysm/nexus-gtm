"""A call may be any step of a campaign, the first included (SDR round 2, W3.1, 2026-10-29).

`validate_steps` and the step editor refused a call as the first step, so a team that opens with a
phone call could not build that sequence. What has to stay true with the rule gone:

* Everyone is still reviewed before their sequence starts: the opening email is the first EMAIL
  step, drafted and approved up front, and approval is what starts the call.
* The call is in the owner's call list the moment the person starts, not on the next heartbeat,
  and exactly once.
* The first email opens a thread with its own subject; every later email answers it with one
  "Re:", whether or not a call sits between them.

Real rows on SQLite, the offline stub model, and the `SentFolder` provider double.
"""
from __future__ import annotations

from datetime import timedelta
from email import message_from_bytes as _parse
from email.policy import default as _modern

import pytest

from nexus.core.config import get_settings
from tests.conftest import tenant_session
from tests.test_engagement_sequences import _enrollment, _run, _world, folder  # noqa: F401

CALL_FIRST = [
    {"channel": "call", "angle": "Introduce yourself before the first email"},
    {"channel": "email", "angle": "Open on the funding round", "delay_business_days": 1},
    {"channel": "email", "angle": "A different reason to talk", "delay_business_days": 2},
]
CALL_BETWEEN = [
    {"channel": "email", "angle": "Open on the funding round"},
    {"channel": "call", "angle": "Call after the first email", "delay_business_days": 1},
    {"channel": "email", "angle": "A different reason to talk", "delay_business_days": 1},
]


def test_a_call_may_lead_but_a_campaign_still_needs_an_email():
    from nexus.engagement.sequences.service import CampaignError, opener_index, validate_steps

    clean = validate_steps(CALL_FIRST)
    assert [s["channel"] for s in clean] == ["call", "email", "email"]
    assert clean[0]["delay_business_days"] == 0, "the first step goes on approval, with no wait"
    assert opener_index(clean) == 1 and opener_index(validate_steps(CALL_BETWEEN)) == 0
    for calls_only in ([{"channel": "call"}], [{"channel": "call"}, {"channel": "call"}]):
        with pytest.raises(CampaignError, match="at least one email"):
            validate_steps(calls_only)


async def _call_first(slug: str, monkeypatch, steps=CALL_FIRST, **options):
    from nexus.engagement.sequences.service import (
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
        review_queue,
    )
    from nexus.models.calling import CallTask
    from nexus.models.engagement import EngagementMessage

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world(slug, contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Call first", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=steps, **options)
        await enroll(ts, campaign, contact_ids)
        assert (await draft_first_emails(ts, campaign, user_id=user_id))["drafted"] == 1
        (enrollment, opener), = await review_queue(ts, campaign)
        # Review shows the opening email, which sits after the call.
        assert opener is not None and opener.step_index == 1 and opener.status == "draft"
        assert not opener.subject.lower().startswith("re:")
        assert await ts.list(CallTask) == [], "nothing starts before the opening email is approved"
        await approve_all_passing(ts, campaign, user_id=user_id)
        await launch(ts, campaign, user_id=user_id)
        drafts = await ts.list(EngagementMessage)
        assert [m.step_index for m in drafts] == [1]
        return tid, user_id, campaign.id, enrollment.id


async def test_a_campaign_that_opens_with_a_call_puts_the_call_in_the_list_at_launch(
        folder, monkeypatch):  # noqa: F811
    from nexus.engagement.sequences.advance import advance
    from nexus.models.calling import CallTask

    tid, user_id, _campaign_id, enrollment_id = await _call_first("callfirst", monkeypatch)
    async with tenant_session(tid) as ts:
        calls = await ts.list(CallTask)
    assert len(calls) == 1
    call = calls[0]
    assert (call.engagement_enrollment_id, call.owner_user_id, call.status) == \
        (enrollment_id, user_id, "open")
    assert call.reason == "Introduce yourself before the first email"
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.current_step_index) == ("active", 1)
    assert folder.delivered == [], "the call goes first; the email waits for its own step"

    # The heartbeat does not create the call a second time.
    await advance(now=enrollment.next_action_at - timedelta(minutes=1))
    async with tenant_session(tid) as ts:
        assert len(await ts.list(CallTask)) == 1


async def test_the_first_email_after_a_call_opens_the_thread_and_the_next_one_answers_it(
        folder, monkeypatch):  # noqa: F811
    from nexus.models.calling import CallTask
    from nexus.models.engagement import EngagementMessage

    tid, *_ = await _call_first("callthread", monkeypatch)
    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == 2 and enrollment.current_thread_id
    first = _parse(folder.delivered[0], policy=_modern)
    assert not first["Subject"].lower().startswith("re:")
    assert not first["In-Reply-To"], "the first email starts a conversation, it answers nothing"

    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent_completed"
    second = _parse(folder.delivered[1], policy=_modern)
    assert second["Subject"] == f"Re: {first['Subject']}"
    assert second["In-Reply-To"] == first["Message-ID"]
    async with tenant_session(tid) as ts:
        sent = await ts.list(EngagementMessage, EngagementMessage.status == "sent")
        assert sorted(m.step_index for m in sent) == [1, 2]
        assert len(await ts.list(CallTask)) == 1


async def test_a_call_between_emails_keeps_the_thread(folder, monkeypatch):  # noqa: F811
    from nexus.engagement.sequences.service import (
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )
    from nexus.models.calling import CallTask

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world("callbetween", contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Between", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=CALL_BETWEEN)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        await launch(ts, campaign, user_id=user_id)
    enrollment = await _enrollment(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) == "sent"
    enrollment = await _enrollment(tid)
    thread_id = enrollment.current_thread_id
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_thread_id == thread_id, "a call keeps the conversation it sits in"
    async with tenant_session(tid) as ts:
        assert len(await ts.list(CallTask)) == 1
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "sent_completed"
    first, second = (_parse(raw, policy=_modern) for raw in folder.delivered)
    assert second["Subject"] == f"Re: {first['Subject']}"
    assert second["In-Reply-To"] == first["Message-ID"]


async def test_a_scheduled_first_touch_creates_the_call_when_it_is_due(folder, monkeypatch):  # noqa: F811
    from nexus.core.db import utcnow
    from nexus.engagement.sequences.advance import advance
    from nexus.models.calling import CallTask

    later = utcnow() + timedelta(days=2)
    tid, *_ = await _call_first("callsched", monkeypatch, first_send_mode="scheduled",
                                first_send_at=later)
    async with tenant_session(tid) as ts:
        assert await ts.list(CallTask) == [], "a scheduled first touch is not due at launch"
    await advance(now=later + timedelta(seconds=1))
    async with tenant_session(tid) as ts:
        assert len(await ts.list(CallTask)) == 1
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == 1 and folder.delivered == []


async def test_the_launch_estimate_does_not_price_a_written_opener_twice(monkeypatch):
    from nexus.engagement.sequences.estimate import emails_left
    from nexus.engagement.sequences.service import create_campaign, draft_first_emails, enroll

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world("callgate", contacts=2)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Gate", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=CALL_FIRST)
        await enroll(ts, campaign, contact_ids)
        assert await emails_left(ts, campaign) == [(2, False), (2, False)]
        await draft_first_emails(ts, campaign, user_id=user_id)
        # Two emails each, the opening one already written (and charged when it was).
        assert await emails_left(ts, campaign) == [(2, True), (2, True)]


async def test_regenerating_the_opener_after_a_call_writes_a_first_email(monkeypatch):
    from nexus.engagement.sequences.service import (
        create_campaign,
        draft_first_emails,
        enroll,
        regenerate,
        review_queue,
    )

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world("callregen", contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Regen", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=CALL_FIRST)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        (_e, opener), = await review_queue(ts, campaign)
        result = await regenerate(ts, opener, user_id=user_id)
        assert result.ok and not opener.subject.lower().startswith("re:")
        assert opener.step_index == 1 and opener.status == "draft"
