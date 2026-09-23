"""What the engagement screens read (spec §9): the engine switch, the candidates a campaign is built
from, the conversation timeline, names on every row, and the scheduled contacts a rep can move.

Real rows throughout; the campaign flow reuses the phase 08 helpers and the phase 09 `Mailbox`
provider double.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_replies import Mailbox, _mail, _sync
from tests.test_engagement_sequences import STEPS, _enrollment, _launched, _run


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


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


# ---- the switch ----------------------------------------------------------------------------------

async def test_the_status_answers_while_everything_else_is_dark(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="scrstatus", email="o@scrstatus.com", company="S")
    r = await client.get("/api/engagement/settings/status", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"engine_on": False, "can_manage": True}
    # The screens it gates are genuinely unreachable while it says so.
    assert (await client.get("/api/engagement/campaigns", headers=auth(token))).status_code == 404

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    rep = await _member(client, token, "rep@scrstatus.com", "rep")
    body = (await client.get("/api/engagement/settings/status", headers=auth(rep))).json()
    assert body == {"engine_on": True, "can_manage": False}


# ---- candidates ----------------------------------------------------------------------------------

async def _book(tid: str):
    """Two accounts on a saved list, one account off it, and people of several kinds."""
    from nexus.core.db import utcnow
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Account, Contact
    from nexus.models.workflow import ListItem, ProspectList

    async with tenant_session(tid) as ts:
        acme, beta, gamma = (Account(name=n, domain=d) for n, d in (
            ("Acme", "acme.io"), ("Beta", "beta.io"), ("Gamma", "gamma.io")))
        for a in (acme, beta, gamma):
            ts.add(a)
        await ts.flush()
        people = {
            "vp": Contact(account_id=acme.id, full_name="Ava VP", title="VP Engineering",
                          seniority="vp", email="ava@acme.io"),
            "ic": Contact(account_id=acme.id, full_name="Ian IC", title="Software Engineer",
                          seniority="entry", email="ian@acme.io"),
            "noemail": Contact(account_id=acme.id, full_name="Nora None", title="CTO",
                               seniority="c_suite", email=None),
            "gone": Contact(account_id=acme.id, full_name="Gail Gone", title="VP Sales",
                            seniority="vp", email="gail@acme.io", deleted_at=utcnow()),
            "named": Contact(account_id=beta.id, full_name="Ned Named", title="Head of Data",
                             seniority="director", email="ned@beta.io"),
            "other": Contact(account_id=beta.id, full_name="Olga Other", title="VP Product",
                             seniority="vp", email="olga@beta.io"),
            "offlist": Contact(account_id=gamma.id, full_name="Omar Off", title="VP Engineering",
                               seniority="vp", email="omar@gamma.io"),
        }
        for c in people.values():
            ts.add(c)
        await ts.flush()
        saved = ProspectList(name="Q4 targets")
        ts.add(saved)
        await ts.flush()
        # Acme as a whole account; at Beta, one named person only.
        ts.add(ListItem(list_id=saved.id, account_id=acme.id))
        ts.add(ListItem(list_id=saved.id, account_id=beta.id, contact_id=people["named"].id))
        await ts.flush()
        await suppress(ts, email="ian@acme.io", reason="unsubscribed")
        return saved.id, {k: v.id for k, v in people.items()}


async def test_a_saved_list_expands_to_the_people_at_its_accounts(client, engine_on):
    token = await signup(client, slug="scrcand", email="o@scrcand.com", company="C")
    me = principal_from_token(token)
    list_id, ids = await _book(me.tenant_id)

    r = await client.get("/api/engagement/candidates", headers=auth(token),
                         params={"list_id": list_id})
    assert r.status_code == 200, r.text
    got = {c["contact_id"]: c for c in r.json()}
    # A whole account brings everyone there with an address; a named item brings that person only.
    # No address, a deleted contact and someone off the list are never offered.
    assert set(got) == {ids["vp"], ids["ic"], ids["named"]}
    # Blocked people are shown and marked, so the SDR can see why they will not be added.
    assert got[ids["ic"]]["blocked"] is True and got[ids["vp"]]["blocked"] is False
    assert got[ids["named"]]["account_name"] == "Beta"


async def test_title_and_seniority_narrow_the_expansion(client, engine_on):
    token = await signup(client, slug="scrfilt", email="o@scrfilt.com", company="F")
    me = principal_from_token(token)
    list_id, ids = await _book(me.tenant_id)
    by_title = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "list_id": list_id, "title": "vp, head of"})).json()
    assert {c["contact_id"] for c in by_title} == {ids["vp"], ids["named"]}
    by_level = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "seniority": "vp"})).json()
    # Without a list, the whole book: every VP with an address, on the list or not.
    assert {c["contact_id"] for c in by_level} == {ids["vp"], ids["other"], ids["offlist"]}
    by_text = (await client.get("/api/engagement/candidates", headers=auth(token), params={
        "q": "gamma"})).json()
    assert [c["contact_id"] for c in by_text] == [ids["offlist"]]


# ---- names on the campaign rows ------------------------------------------------------------------

async def test_the_review_queue_and_the_contacts_say_who_each_row_is(client, engine_on,
                                                                   monkeypatch):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from tests.test_engagement_sequences import seed_relevance_profile

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug="scrnames", email="sam@scrnames.com", company="N")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await seed_relevance_profile(ts)
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@scrnames.com", status="connected")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io",
                          title="VP Engineering")
        ts.add(contact)
        await ts.flush()
        mailbox_id, contact_id = mailbox.id, contact.id

    created = (await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Q4", "mailbox_id": mailbox_id, "steps": STEPS})).json()
    await client.post(f"/api/engagement/campaigns/{created['id']}/contacts", headers=auth(token),
                      json={"contact_ids": [contact_id]})
    expected = ("Jane Buyer", "jane@acme.io", "VP Engineering", "Acme Robotics")
    review = (await client.get(f"/api/engagement/campaigns/{created['id']}/review",
                               headers=auth(token))).json()
    row = review[0]
    assert (row["contact_name"], row["contact_email"], row["contact_title"],
            row["account_name"]) == expected
    enrollments = (await client.get(f"/api/engagement/campaigns/{created['id']}/enrollments",
                                    headers=auth(token))).json()
    row = enrollments[0]
    assert (row["contact_name"], row["contact_email"], row["contact_title"],
            row["account_name"]) == expected


# ---- the conversation timeline -------------------------------------------------------------------

async def test_the_timeline_shows_what_was_sent_and_what_came_back(mailbox_double, monkeypatch):
    from email import message_from_bytes

    from nexus.engagement.sequences.candidates import timeline

    tid, campaign_id = await _launched("scrtime", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    our_id = message_from_bytes(mailbox_double.delivered[0])["Message-ID"]
    # The reply arrives after the send, in real time: the fixture's default date is in the past.
    arrived = datetime.now(UTC) + timedelta(minutes=5)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id, when=arrived,
                                body="Sounds interesting, let's talk next week."), when=arrived)
    await _sync(tid, enrollment.mailbox_connection_id)
    async with tenant_session(tid) as ts:
        by_contact = await timeline(ts, contact_id=enrollment.contact_id)
        by_account = await timeline(ts, account_id=enrollment.account_id)
    assert [e.direction for e in by_contact] == ["out", "in"]
    assert by_contact[0].campaign_id == campaign_id and by_contact[0].campaign_name == "Q4"
    # The reply carries what it was read as, so the timeline says "interested" without a click.
    assert by_contact[1].category == "interested"
    assert [e.message_id for e in by_account] == [e.message_id for e in by_contact]


# ---- scheduled contacts --------------------------------------------------------------------------

async def test_a_scheduled_contact_can_be_given_a_new_date_or_cancelled(mailbox_double,
                                                                       monkeypatch):
    from nexus.engagement.desk.service import DeskError, cancel_scheduled, reschedule
    from nexus.models.engagement import EngagementEnrollment

    tid, _campaign = await _launched("scrsched", monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    from email import message_from_bytes

    our_id = message_from_bytes(mailbox_double.delivered[0])["Message-ID"]
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not now — try me in June please."))
    await _sync(tid, enrollment.mailbox_connection_id)
    new_date = datetime.now(UTC) + timedelta(days=30)
    async with tenant_session(tid) as ts:
        row = await ts.get(EngagementEnrollment, enrollment.id)
        assert row.status == "snoozed"
        await reschedule(ts, row, new_date, user_id="u1")
        assert row.snoozed_until == new_date
        with pytest.raises(DeskError):
            await reschedule(ts, row, datetime.now(UTC) - timedelta(days=1), user_id="u1")
        await cancel_scheduled(ts, row, user_id="u1")
        assert row.status == "stopped"
        # A stopped contact is no longer waiting for anything.
        with pytest.raises(DeskError):
            await reschedule(ts, row, new_date, user_id="u1")


async def test_a_rep_cannot_steer_a_contact_in_a_colleagues_mailbox(client, engine_on):
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    owner = await signup(client, slug="scrown", email="boss@scrown.com", company="O")
    me = principal_from_token(owner)
    ann = await _member(client, owner, "ann@scrown.com", "rep")
    bob = await _member(client, owner, "bob@scrown.com", "rep")
    ann_id = principal_from_token(ann).user_id
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=ann_id, provider="google",
                                    email="ann@scrown.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane", email="jane@acme.io")
        campaign = EngagementCampaign(name="Ann's", owner_user_id=ann_id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(contact)
        ts.add(campaign)
        await ts.flush()
        enrollment = EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=account.id,
            mailbox_connection_id=mailbox.id, status="snoozed",
            snoozed_until=datetime.now(UTC) + timedelta(days=5))
        ts.add(enrollment)
        await ts.flush()
        enrollment_id = enrollment.id

    later = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    for token, expected in ((bob, 404), (ann, 204)):
        r = await client.post(f"/api/engagement/desk/scheduled/{enrollment_id}/reschedule",
                              headers=auth(token), json={"when": later})
        assert r.status_code == expected, r.text
    # The colleague route had no ownership check at all; it now answers like the rest.
    r = await client.post(f"/api/engagement/desk/colleagues/{enrollment_id}/stop",
                          headers=auth(bob))
    assert r.status_code == 404
