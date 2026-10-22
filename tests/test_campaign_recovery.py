"""People must not silently stop being emailed (debugging pass, 2026-10-22).

Each test here is a way a running campaign stopped reaching someone, or refused to carry on, while
the screen said it was fine:

* a reconnected mailbox left everyone it had paused paused for good: the campaign read "Running",
  the banner offering a mailbox only appears while the mailbox is NOT connected, and Platform health
  counts only what is due, which a paused person never is;
* resuming after the credits ran out left the person whose email hit the wall paused for good,
  under a notice promising "it continues from where it stopped";
* a follow-up held for review (D17, or review every touch) and approved while the campaign was
  paused was looked for at step 0 on resume, so it never went;
* the resume gate priced every step for every person, sent or not, so a top-up that covered what
  was left was still refused;
* a list bigger than one add lost the rest without a word, and adding it again re-took the same
  first 2,000, so the rest could never be added;
* drafting and adding people read once per person, so a long list made every pass slower.

Drafts come from the real `MessagingAgent` on the offline stub; sends go to the `SentFolder` double.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from nexus.core.config import get_settings
from nexus.core.db import get_engine
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import STEPS, _world


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


@contextmanager
def _count_statements():
    seen: list[str] = []

    def before(conn, cursor, statement, *_):
        seen.append(statement)

    sync = get_engine().sync_engine
    event.listen(sync, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(sync, "before_cursor_execute", before)


async def _launch(slug: str, monkeypatch, *, contacts: int = 1, **options):
    from nexus.engagement.sequences.service import (
        approve_all_passing,
        create_campaign,
        draft_first_emails,
        enroll,
        launch,
    )

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid, user_id, mailbox_id, contact_ids = await _world(slug, contacts=contacts)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS, **options)
        await enroll(ts, campaign, contact_ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        await approve_all_passing(ts, campaign, user_id=user_id)
        await launch(ts, campaign, user_id=user_id)
        return tid, user_id, campaign.id


async def _enrollments(tid):
    from nexus.models.engagement import EngagementEnrollment

    async with tenant_session(tid) as ts:
        return sorted(await ts.list(EngagementEnrollment), key=lambda e: e.created_at)


async def _run(tid, enrollment_id, when):
    from nexus.engagement.sequences.advance import process

    async with tenant_session(tid) as ts:
        return await process(ts, enrollment_id, now=when)


# ---- a mailbox that comes back ---------------------------------------------------------------------

async def test_a_reconnected_mailbox_resumes_everyone_it_paused(folder, monkeypatch):
    from nexus.engagement.sequences.advance import due_enrollments
    from nexus.models.engagement import MailboxConnection

    tid, _user, _campaign = await _launch("reconnect", monkeypatch)
    (enrollment,) = await _enrollments(tid)
    due = enrollment.next_action_at + timedelta(seconds=1)
    async with tenant_session(tid) as ts:
        (await ts.get(MailboxConnection, enrollment.mailbox_connection_id)).status = "needs_reauth"
    assert await _run(tid, enrollment.id, due) == "mailbox_disconnected"

    # Still disconnected: nothing to pick up.
    later = due + timedelta(hours=2)
    assert (tid, enrollment.id) not in await due_enrollments(later)

    async with tenant_session(tid) as ts:     # the SDR reconnects (or the app is fixed)
        (await ts.get(MailboxConnection, enrollment.mailbox_connection_id)).status = "connected"
    assert (tid, enrollment.id) in await due_enrollments(later)
    assert await _run(tid, enrollment.id, later) == "sent"
    (enrollment,) = await _enrollments(tid)
    assert enrollment.status == "active" and enrollment.current_step_index == 1
    assert len(folder.delivered) == 1


# ---- credits running out, then a top-up -------------------------------------------------------------

async def test_resuming_after_credits_ran_out_resumes_the_person_who_hit_the_wall(folder, monkeypatch):
    from nexus.engagement.sequences.advance import _out_of_credits, due_enrollments
    from nexus.engagement.sequences.service import resume_campaign
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    tid, user_id, campaign_id = await _launch("walled", monkeypatch, contacts=2)
    walled, other = await _enrollments(tid)
    async with tenant_session(tid) as ts:
        campaign = await ts.get(EngagementCampaign, campaign_id)
        await _out_of_credits(ts, campaign, await ts.get(EngagementEnrollment, walled.id))
        assert campaign.status == "paused"
    async with tenant_session(tid) as ts:
        await resume_campaign(ts, await ts.get(EngagementCampaign, campaign_id), user_id=user_id)

    walled, other = await _enrollments(tid)
    assert (walled.status, walled.status_reason) == ("active", None)
    assert other.status == "active"
    later = max(walled.next_action_at, other.next_action_at) + timedelta(seconds=1)
    due = await due_enrollments(later)
    assert (tid, walled.id) in due and (tid, other.id) in due


async def test_a_follow_up_approved_while_paused_goes_out_after_resume(folder, monkeypatch):
    from nexus.engagement.sequences.service import (
        approve,
        pause_campaign,
        resume_campaign,
        review_queue,
    )
    from nexus.models.engagement import EngagementCampaign

    tid, user_id, campaign_id = await _launch("heldpause", monkeypatch, review_every_touch=True)
    (enrollment,) = await _enrollments(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) == "sent"
    (enrollment,) = await _enrollments(tid)
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) \
        == "awaiting_review"

    async with tenant_session(tid) as ts:
        campaign = await ts.get(EngagementCampaign, campaign_id)
        await pause_campaign(ts, campaign)
        ((_e, held),) = await review_queue(ts, campaign)
        assert held.step_index == 1 and held.status == "draft"
        await approve(ts, held, user_id=user_id)
    async with tenant_session(tid) as ts:
        await resume_campaign(ts, await ts.get(EngagementCampaign, campaign_id), user_id=user_id)

    (enrollment,) = await _enrollments(tid)
    assert enrollment.status == "active" and enrollment.next_action_at is not None
    assert await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1)) == "sent"
    assert len(folder.delivered) == 2


# ---- the gate prices what is left ------------------------------------------------------------------

async def test_the_resume_gate_prices_only_what_is_left_to_send(folder, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.sequences.estimate import estimate
    from nexus.engagement.sequences.service import set_status
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    await sync_catalog()
    await sync_rates()
    tid, _user, campaign_id = await _launch("leftover", monkeypatch, contacts=2)
    first, second = await _enrollments(tid)
    for e in (first, second):
        assert await _run(tid, e.id, e.next_action_at + timedelta(seconds=1)) == "sent"

    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    async with tenant_session(tid) as ts:
        result = await estimate(ts, await ts.get(EngagementCampaign, campaign_id))
    per = result.per_capability
    # Two people, two follow-ups each still to write and send: 4 x (draft 2 + send 1) = 12. The old
    # estimate priced all three steps again, 18, though the openers had gone.
    assert per["ai.email_draft"]["units"] == 4 and per["outreach.email_send"]["units"] == 4
    assert per["ai.email_draft"]["credits"] + per["outreach.email_send"]["credits"] == 12
    assert result.contacts == 2

    async with tenant_session(tid) as ts:
        await set_status(ts, await ts.get(EngagementEnrollment, second.id), "completed")
    async with tenant_session(tid) as ts:
        result = await estimate(ts, await ts.get(EngagementCampaign, campaign_id))
    assert result.per_capability["outreach.email_send"]["units"] == 2
    assert result.per_capability["ai.reply_classify"]["units"] == 1
    assert result.contacts == 1


async def test_a_written_opener_is_not_priced_again_at_launch(monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.engagement.sequences.estimate import estimate
    from nexus.engagement.sequences.service import create_campaign, draft_first_emails, enroll

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid, user_id, mailbox_id, contact_ids = await _world("opener", contacts=2)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        before = await estimate(ts, campaign)
        await draft_first_emails(ts, campaign, user_id=user_id)
        after = await estimate(ts, campaign)
    assert before.per_capability["ai.email_draft"]["units"] == 6
    # The two openers are written (and were charged when written); sending them is still ahead.
    assert after.per_capability["ai.email_draft"]["units"] == 4
    assert after.per_capability["outreach.email_send"]["units"] == 6


# ---- adding a list bigger than one add -------------------------------------------------------------

async def test_a_list_bigger_than_one_add_says_how_many_are_left_and_the_next_add_takes_them(
        client, monkeypatch):
    from nexus.engagement.sequences import service
    from nexus.lists import service as lists
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(service, "MAX_PER_ADD", 3)
    token = await signup(client, slug="biglist", email="sam@biglist.com", company="Big")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@biglist.com", status="connected")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        people = []
        for i in range(5):
            c = Contact(account_id=account.id, full_name=f"P{i} Person", email=f"p{i}@acme.io")
            ts.add(c)
            await ts.flush()
            people.append(c.id)
        plist = await lists.create(ts, name="Everyone", kind="contact", owner_user_id=me.user_id)
        await lists.add_members(ts, plist, contact_ids=people)
        mailbox_id, list_id = mailbox.id, plist.id

    created = await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Big", "mailbox_id": mailbox_id, "steps": STEPS})
    campaign_id = created.json()["id"]
    url = f"/api/engagement/campaigns/{campaign_id}/contacts"

    first = (await client.post(url, headers=auth(token), json={"list_id": list_id})).json()
    assert len(first["added"]) == 3 and first["remaining"] == 2
    second = (await client.post(url, headers=auth(token), json={"list_id": list_id})).json()
    assert len(second["added"]) == 2 and second["remaining"] == 0
    assert set(first["added"]) | set(second["added"]) == set(people)
    # Nothing new left: the server says so rather than "nobody to add".
    third = (await client.post(url, headers=auth(token), json={"list_id": list_id})).json()
    assert third["added"] == [] and third["remaining"] == 0


# ---- a fixed number of reads whatever the list size -------------------------------------------------

async def test_adding_people_reads_a_fixed_number_of_times_whatever_the_count(monkeypatch):
    from nexus.engagement.sequences.service import create_campaign, enroll
    from nexus.models.account import Contact

    tid, user_id, mailbox_id, contact_ids = await _world("enrolreads", contacts=40)
    async with tenant_session(tid) as ts:
        other = await create_campaign(ts, name="Earlier", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS[:1])
        await enroll(ts, other, contact_ids[:20])      # duplicate-outreach warnings for half
        small = await create_campaign(ts, name="Small", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        large = await create_campaign(ts, name="Large", owner_user_id=user_id,
                                      mailbox_id=mailbox_id, steps=STEPS)
        nobody = await ts.get(Contact, contact_ids[-1])
        nobody.email = ""
        await ts.flush()
        with _count_statements() as few:
            got_small = await enroll(ts, small, contact_ids[:5])
        with _count_statements() as many:
            got_large = await enroll(ts, large, contact_ids)
    assert len(got_small.added) == 5
    assert len(got_large.added) == 39 and got_large.skipped == [
        {"contact_id": contact_ids[-1], "reason": "no_email"}]
    # The first 20 are live in "Earlier", and the first 5 in "Small" as well.
    assert len(got_large.warnings) == 25
    assert {w["campaign_name"] for w in got_large.warnings} == {"Earlier", "Small"}
    assert {w["owner"] for w in got_large.warnings} == {"Sam Rep"}
    assert len(many) - len(few) <= 3, f"{len(few)} reads for 5 people, {len(many)} for 40"


async def test_a_drafting_pass_does_not_reread_everyone_already_drafted(monkeypatch):
    from nexus.engagement.sequences.service import (
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )

    tid, user_id, mailbox_id, contact_ids = await _world("draftreads", contacts=12)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, contact_ids)
        assert (await draft_first_emails(ts, campaign, user_id=user_id, limit=5))["drafted"] == 5
        assert (await draft_first_emails(ts, campaign, user_id=user_id))["drafted"] == 7
        with _count_statements() as idle:
            assert (await draft_first_emails(ts, campaign, user_id=user_id))["drafted"] == 0
        with _count_statements() as queue_reads:
            rows = await review_queue(ts, campaign)
    assert len(idle) <= 4, f"a pass with nothing to draft read {len(idle)} times"
    assert len(rows) == 12 and all(m is not None for _e, m in rows)
    assert len(queue_reads) <= 3, f"the review queue read {len(queue_reads)} times for 12 people"
