"""The cutover from the old Campaigns and Cadences engines (spec §13, D13, D14).

Real old-engine rows through `tenant_session`, migrated by the real script's core, with the Sent
folder served by the provider double the engagement suites use (D21): it answers `search_sent` for
the emails it knows and nothing else, as a mailbox would.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup, tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import NOW, _world


class SentHistory(SentFolder):
    """A mailbox whose Sent folder holds some of the old engine's emails."""

    def __init__(self):
        super().__init__()
        self.known: dict[tuple[str, str], object] = {}
        self.searched: list[tuple[str, str]] = []

    def remember(self, to: str, subject: str, thread_id: str) -> None:
        from nexus.engagement.mailboxes.provider import SentRef

        self.known[(to, subject)] = SentRef(provider_message_id=f"pm-{len(self.known) + 1}",
                                            provider_thread_id=thread_id)

    async def search_sent(self, *, to: str, subject: str, around):
        self.searched.append((to, subject))
        return self.known.get((to, subject))


@pytest.fixture
def history():
    from nexus.engagement.mailboxes import registry

    box = SentHistory()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


# ---- pure conversions ----------------------------------------------------------------------------

def test_calendar_waits_become_business_days():
    from nexus.engagement.cutover.migrate import business_days

    assert [business_days(d) for d in (0, 1, 2, 3, 7, 14, 200)] == [0, 1, 1, 2, 5, 10, 60]


# ---- a workspace mid-flight on the old engine ----------------------------------------------------

async def _old_engine(slug: str):
    """Sam (a connected mailbox) runs a three-step cadence campaign: Jane0 is on step 2 with two
    emails sent, Jane1 on step 1. Kim (no mailbox) runs one with Jane2 on step 1. A third campaign
    awaits approval with a draft for Jane3, and a fourth finished long ago."""
    from nexus.models.account import Contact
    from nexus.models.cadence import Cadence, CadenceEnrollment, CadenceStep, CadenceTouch
    from nexus.models.calling import CallTask
    from nexus.models.campaign import Campaign, CampaignTarget
    from nexus.models.identity import Membership, User
    from nexus.models.workflow import ProspectList

    tid, sam_id, mailbox_id, contacts = await _world(slug, contacts=4)
    async with tenant_session(tid) as ts:
        kim = User(email=f"kim@{slug}.com", full_name="Kim Rep", password_hash="x")
        ts.session.add(kim)
        await ts.session.flush()
        ts.add(Membership(user_id=kim.id, role="rep"))
        cadence = Cadence(name="Three touches", description="Old cadence",
                          created_by_user_id=sam_id)
        ts.add(cadence)
        await ts.flush()
        for i, (days, angle) in enumerate(((0, "Open"), (3, "Bump"), (7, "Break-up"))):
            ts.add(CadenceStep(cadence_id=cadence.id, step_index=i, delay_days=days, angle=angle))
        saved = ProspectList(name="Q3 list", owner_user_id=sam_id)
        ts.add(saved)
        await ts.flush()

        def campaign(name, status, owner, with_cadence=True):
            row = Campaign(name=name, list_id=saved.id, status=status, created_by_user_id=owner,
                           cadence_id=cadence.id if with_cadence else None)
            ts.add(row)
            return row

        running = campaign("Q3 outbound", "sending", sam_id)
        kims = campaign("Kim's outbound", "sending", kim.id)
        waiting = campaign("Q4 draft", "awaiting_approval", sam_id, with_cadence=False)
        campaign("Q2 done", "completed", sam_id)
        await ts.flush()
        account_id = (await ts.get(Contact, contacts[0])).account_id

        def enrolled(camp, contact_id, step, due):
            row = CadenceEnrollment(campaign_id=camp.id, account_id=account_id,
                                    contact_id=contact_id, cadence_id=cadence.id,
                                    current_step_index=step, status="active",
                                    next_touch_at=due, started_at=NOW - timedelta(days=10))
            ts.add(row)
            return row

        jane0 = enrolled(running, contacts[0], 2, NOW + timedelta(days=2))
        enrolled(running, contacts[1], 1, NOW + timedelta(days=1))
        enrolled(kims, contacts[2], 1, NOW + timedelta(days=1))
        await ts.flush()
        for step, subject in ((0, "Quick question"), (1, "Re: Quick question")):
            ts.add(CadenceTouch(enrollment_id=jane0.id, step_index=step, status="sent",
                                draft={"subject": subject, "body": f"Touch {step}"},
                                sent_at=NOW - timedelta(days=9 - step * 3)))
        ts.add(CallTask(account_id=account_id, contact_id=contacts[0], reason="Cadence call",
                        owner_user_id=sam_id, cadence_enrollment_id=jane0.id,
                        cadence_step_index=2))
        ts.add(CampaignTarget(campaign_id=waiting.id, account_id=account_id, status="drafted",
                              draft={"contact_id": contacts[3], "subject": "Hello Jane3",
                                     "body": "An opening email."}))
        await ts.flush()
        return tid, sam_id, kim.id, contacts, running.id, jane0.id


async def _counts(tid):
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        SequenceTemplate,
    )

    async with tenant_session(tid) as ts:
        return tuple([len(await ts.list(m)) for m in (SequenceTemplate, EngagementCampaign,
                                                       EngagementEnrollment, EngagementMessage)])


async def test_the_dry_run_reports_what_would_move_and_writes_nothing(history):
    from nexus.engagement.cutover.migrate import migrate, render

    tid, *_ = await _old_engine("cutdry")
    history.remember("jane0@acme.io", "Quick question", "thread-q")
    async with tenant_session(tid) as ts:
        report = await migrate(ts, dry_run=True, now=NOW)
    assert report.templates_created == 1
    assert report.totals == {"sequences_moved": 3, "paused_for_mailbox": 1, "drafts_to_review": 1,
                             "emails_found": 1, "emails_not_found": 1}
    assert report.call_tasks_linked == 1 and report.legacy_campaigns == 1
    text = render(report)
    assert "DRY RUN, nothing written" in text and "1 paused until a mailbox is connected" in text
    assert await _counts(tid) == (0, 0, 0, 0), "a dry run writes nothing"


async def test_the_real_run_moves_sequences_at_the_same_step_and_twice_moves_nothing(history):
    from nexus.engagement.cutover.migrate import migrate
    from nexus.models.calling import CallTask
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        EngagementThread,
        SequenceTemplate,
    )

    tid, sam_id, kim_id, contacts, running_id, jane0_old = await _old_engine("cutreal")
    history.remember("jane0@acme.io", "Quick question", "thread-q")
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)

    async with tenant_session(tid) as ts:
        template = await ts.first(SequenceTemplate)
        assert [s["delay_business_days"] for s in template.steps] == [0, 2, 5]
        running = await ts.first(EngagementCampaign,
                                 EngagementCampaign.legacy_campaign_id == running_id)
        assert (running.status, running.owner_user_id) == ("active", sam_id)
        jane0 = await ts.first(EngagementEnrollment,
                               EngagementEnrollment.legacy_enrollment_id == jane0_old)
        # The same step, the same due time: nothing is re-sent and nothing is skipped.
        assert (jane0.status, jane0.current_step_index) == ("active", 2)
        assert jane0.next_action_at.replace(tzinfo=None) == (NOW + timedelta(days=2)).replace(
            tzinfo=None)
        sent = sorted(await ts.list(EngagementMessage,
                                    EngagementMessage.enrollment_id == jane0.id),
                      key=lambda m: m.step_index)
        assert [(m.step_index, m.status, m.subject) for m in sent] == [
            (0, "sent", "Quick question"), (1, "sent", "Re: Quick question")]
        thread = await ts.get(EngagementThread, jane0.current_thread_id)
        assert thread.provider_thread_id == "thread-q", "replies will find the found thread"
        assert sent[0].thread_id == thread.id and sent[1].thread_id is None
        call = await ts.first(CallTask, CallTask.cadence_enrollment_id == jane0_old)
        assert call.engagement_enrollment_id == jane0.id

        kims = await ts.first(EngagementCampaign, EngagementCampaign.owner_user_id == kim_id)
        assert (kims.status, kims.pause_reason, kims.mailbox_connection_id) == (
            "paused", "mailbox_disconnected", None)
        waiting = await ts.first(EngagementCampaign, EngagementCampaign.name == "Q4 draft")
        drafts = await ts.list(EngagementMessage, EngagementMessage.status == "draft")
        assert waiting.status == "reviewing" and [d.subject for d in drafts] == ["Hello Jane3"]
        assert await ts.first(EngagementCampaign, EngagementCampaign.name == "Q2 done") is None

    before = await _counts(tid)
    async with tenant_session(tid) as ts:
        again = await migrate(ts, dry_run=False, now=NOW)
    assert again.templates_created == 0 and again.totals["sequences_moved"] == 0
    assert await _counts(tid) == before, "a second run moves nothing twice"


async def test_a_sequence_waiting_for_a_mailbox_moves_on_when_one_is_connected(history):
    from nexus.engagement.cutover.migrate import migrate
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment, MailboxConnection

    tid, _sam, kim_id, *_ = await _old_engine("cutlater")
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)
    async with tenant_session(tid) as ts:
        ts.add(MailboxConnection(owner_user_id=kim_id, provider="google", email="kim@x.com",
                                 status="connected", timezone="UTC"))
    async with tenant_session(tid) as ts:
        await migrate(ts, dry_run=False, now=NOW)
    async with tenant_session(tid) as ts:
        kims = await ts.first(EngagementCampaign, EngagementCampaign.owner_user_id == kim_id)
        person = await ts.first(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == kims.id)
        assert (kims.status, kims.mailbox_connection_id is not None) == ("active", True)
        assert (person.status, person.status_reason) == ("active", None)


async def test_a_campaign_can_be_given_its_owners_mailbox_from_the_screen(client, monkeypatch):
    from nexus.core.db import utcnow
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )
    from nexus.models.account import Account, Contact
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    token = await signup(client, slug="cutbox", email="sam@cutbox.com", company="C")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        mine = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                 email="sam@cutbox.com", status="connected", timezone="UTC")
        ts.add(contact)
        ts.add(mine)
        await ts.flush()
        campaign = EngagementCampaign(name="Moved", owner_user_id=me.user_id, status="paused",
                                      pause_reason="mailbox_disconnected")
        ts.add(campaign)
        await ts.flush()
        ts.add(EngagementEnrollment(campaign_id=campaign.id, contact_id=contact.id,
                                    account_id=account.id, status="paused",
                                    status_reason="mailbox_disconnected",
                                    next_action_at=utcnow()))
        ids = (campaign.id, mine.id)

    r = await client.put(f"/api/engagement/campaigns/{ids[0]}/mailbox", headers=auth(token),
                         json={"mailbox_id": ids[1]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    async with tenant_session(me.tenant_id) as ts:
        person = await ts.first(EngagementEnrollment)
        assert (person.status, person.mailbox_connection_id) == ("active", ids[1])

    bad = await client.put(f"/api/engagement/campaigns/{ids[0]}/mailbox", headers=auth(token),
                           json={"mailbox_id": "nope"})
    assert bad.status_code == 409


async def test_finished_old_campaigns_are_listed_read_only(client, monkeypatch):
    from nexus.models.campaign import Campaign, CampaignTarget
    from nexus.models.account import Account
    from nexus.models.workflow import ProspectList
    from tests.conftest import principal_from_token

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    token = await signup(client, slug="cutlegacy", email="sam@cutlegacy.com", company="L")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        saved = ProspectList(name="Old", owner_user_id=me.user_id)
        account = Account(name="Acme", domain="acme.io")
        ts.add(saved)
        ts.add(account)
        await ts.flush()
        done = Campaign(name="Q2 done", list_id=saved.id, status="completed",
                        created_by_user_id=me.user_id)
        ts.add(done)
        ts.add(Campaign(name="Still running", list_id=saved.id, status="sending",
                        created_by_user_id=me.user_id))
        await ts.flush()
        for status_ in ("sent", "sent", "skipped"):
            ts.add(CampaignTarget(campaign_id=done.id, account_id=account.id, status=status_))

    r = await client.get("/api/engagement/legacy-campaigns", headers=auth(token))
    assert r.status_code == 200, r.text
    assert [(c["name"], c["status"], c["targets"], c["sent"]) for c in r.json()] == [
        ("Q2 done", "completed", 3, 2)]


# ---- the old engine stays gone -------------------------------------------------------------------

def test_old_engine_is_gone():
    """D13: the old code paths are removed; only their tables remain, as history. An import of the
    old packages coming back would resurrect a second engine sending to the same people."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "nexus"
    assert not (root / "campaigns").exists() and not (root / "cadences").exists()
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if "nexus.campaigns" in (text := p.read_text(encoding="utf-8"))
                 or "nexus.cadences" in text]
    assert offenders == [], f"imports of the removed engine in {offenders}"
