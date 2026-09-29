"""Lists you build by hand: account lists and contact lists, their members, and who may change them.

A list used to be only a saved filter over accounts: created once, never opened, never edited, and
unable to hold a person. These pin the page that replaced that: a list has a kind, holds exactly the
members someone put in it, and is archived rather than deleted because old campaigns still point at
its row.
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0], "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


async def _book(token: str) -> dict:
    """Two accounts, three people (one without an address), all in the caller's workspace."""
    from nexus.models.account import Account, Contact

    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:
        acme, beta = Account(name="Acme", domain="acme.io"), Account(name="Beta", domain="beta.io")
        ts.add(acme)
        ts.add(beta)
        await ts.flush()
        ava = Contact(account_id=acme.id, full_name="Ava Buyer", title="VP Sales",
                      email="ava@acme.io")
        ian = Contact(account_id=acme.id, full_name="Ian User", title="Engineer",
                      email="ian@acme.io")
        ned = Contact(account_id=beta.id, full_name="Ned Nomail", title="CTO", email=None)
        for c in (ava, ian, ned):
            ts.add(c)
        await ts.flush()
        return {"acme": acme.id, "beta": beta.id, "ava": ava.id, "ian": ian.id, "ned": ned.id}


async def _create(client, token: str, name: str, kind: str, **ids) -> dict:
    r = await client.post("/api/lists", headers=auth(token), json={"name": name, "kind": kind, **ids})
    assert r.status_code == 201, r.text
    return r.json()


# ---- creating and filling -------------------------------------------------------------------------

async def test_a_contact_list_holds_the_people_put_in_it(client):
    token = await signup(client, slug="lmc", email="o@lmc.io", company="Lmc")
    book = await _book(token)
    made = await _create(client, token, "Champions", "contact", contact_ids=[book["ava"]])
    assert made["kind"] == "contact" and made["members"] == 1

    added = await client.post(f"/api/lists/{made['id']}/members", headers=auth(token),
                              json={"contact_ids": [book["ava"], book["ian"]]})
    assert added.status_code == 200, added.text
    # Adding someone already there is reported, never duplicated.
    assert added.json() == {"added": 1, "already": 1, "skipped": 0, "members": 2}

    page = (await client.get(f"/api/lists/{made['id']}/members", headers=auth(token))).json()
    assert page["total"] == 2
    people = {m["contact_id"]: m for m in page["items"]}
    assert set(people) == {book["ava"], book["ian"]}
    assert people[book["ava"]]["full_name"] == "Ava Buyer"
    assert people[book["ava"]]["account_name"] == "Acme"

    listed = (await client.get("/api/lists?kind=contact", headers=auth(token))).json()
    row = next(item for item in listed if item["id"] == made["id"])
    # `accounts` stays the number of distinct companies, which the campaign picker already reads.
    assert row["members"] == 2 and row["accounts"] == 1 and row["can_edit"] is True


async def test_an_account_list_is_built_by_hand_and_members_can_be_removed(client):
    token = await signup(client, slug="lma", email="o@lma.io", company="Lma")
    book = await _book(token)
    made = await _create(client, token, "Tier 1", "account")
    assert made["members"] == 0

    r = await client.post(f"/api/lists/{made['id']}/members", headers=auth(token),
                          json={"account_ids": [book["acme"], book["beta"]]})
    assert r.json()["added"] == 2
    page = (await client.get(f"/api/lists/{made['id']}/members", headers=auth(token))).json()
    assert {m["account_id"] for m in page["items"]} == {book["acme"], book["beta"]}
    assert all(m["contact_id"] is None for m in page["items"])

    removed = await client.post(f"/api/lists/{made['id']}/members/remove", headers=auth(token),
                                json={"account_ids": [book["beta"]]})
    assert removed.status_code == 200 and removed.json() == {"removed": 1, "members": 1}


async def test_a_list_refuses_members_of_the_other_kind(client):
    token = await signup(client, slug="lmk", email="o@lmk.io", company="Lmk")
    book = await _book(token)
    accounts = await _create(client, token, "Companies", "account")
    r = await client.post(f"/api/lists/{accounts['id']}/members", headers=auth(token),
                          json={"contact_ids": [book["ava"]]})
    assert r.status_code == 422
    assert "account list" in r.json()["detail"]


async def test_another_workspaces_records_are_skipped_not_added(client):
    mine = await signup(client, slug="lmx1", email="o@lmx1.io", company="Mine")
    theirs = await signup(client, slug="lmx2", email="o@lmx2.io", company="Theirs")
    their_book = await _book(theirs)
    made = await _create(client, mine, "Mine", "contact")
    r = await client.post(f"/api/lists/{made['id']}/members", headers=auth(mine),
                          json={"contact_ids": [their_book["ava"]]})
    assert r.json() == {"added": 0, "already": 0, "skipped": 1, "members": 0}
    # And the other workspace cannot see this list at all.
    assert (await client.get(f"/api/lists/{made['id']}", headers=auth(theirs))).status_code == 404


async def test_a_filter_list_still_builds_and_is_an_account_list(client):
    """The Relevance filter builder keeps working exactly as before, and says what kind it made."""
    token = await signup(client, slug="lmf", email="o@lmf.io", company="Lmf")
    await _book(token)
    r = await client.post("/api/lists", headers=auth(token),
                          json={"name": "Everything", "filter": {}})
    assert r.status_code == 201, r.text
    assert r.json()["accounts"] == 2 and r.json()["kind"] == "account"


# ---- who may change a list ------------------------------------------------------------------------

async def test_a_rep_edits_their_own_list_but_not_a_colleagues(client, no_auth_rate_limit):
    owner = await signup(client, slug="lmp", email="o@lmp.io", company="Lmp")
    rep = await _member(client, owner, "rep@lmp.io", "rep")
    other = await _member(client, owner, "rep2@lmp.io", "rep")
    manager = await _member(client, owner, "mgr@lmp.io", "manager")

    theirs = await _create(client, other, "Rep two's", "account")
    mine = await _create(client, rep, "Rep one's", "account")

    assert (await client.patch(f"/api/lists/{mine['id']}", headers=auth(rep),
                               json={"name": "Renamed"})).status_code == 200
    assert (await client.patch(f"/api/lists/{theirs['id']}", headers=auth(rep),
                               json={"name": "Mine now"})).status_code == 403
    assert (await client.delete(f"/api/lists/{theirs['id']}",
                                headers=auth(rep))).status_code == 403
    listed = (await client.get("/api/lists", headers=auth(rep))).json()
    flags = {item["id"]: item["can_edit"] for item in listed}
    assert flags == {mine["id"]: True, theirs["id"]: False}
    # A manager looks after the team's lists.
    assert (await client.patch(f"/api/lists/{theirs['id']}", headers=auth(manager),
                               json={"name": "Tidied"})).status_code == 200


# ---- deleting -------------------------------------------------------------------------------------

async def test_deleting_a_list_archives_it_and_frees_its_accounts(client):
    """The row stays because the old campaigns table and engagement campaigns reference it; the
    members go, so an account is no longer kept on the hot refresh cycle by a list nobody has."""
    from nexus.ingestion.tiering import _on_a_list  # noqa: PLC0415
    from nexus.models.workflow import ProspectList

    token = await signup(client, slug="lmd", email="o@lmd.io", company="Lmd")
    book = await _book(token)
    made = await _create(client, token, "Short-lived", "account", account_ids=[book["acme"]])
    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:
        assert await _on_a_list(ts, book["acme"])

    assert (await client.delete(f"/api/lists/{made['id']}", headers=auth(token))).status_code == 204
    assert (await client.get(f"/api/lists/{made['id']}", headers=auth(token))).status_code == 404
    assert made["id"] not in {item["id"] for item in
                              (await client.get("/api/lists", headers=auth(token))).json()}
    async with tenant_session(tid) as ts:
        assert not await _on_a_list(ts, book["acme"])
        row = await ts.get(ProspectList, made["id"])
        assert row is not None and row.archived_at is not None


# ---- campaigns ------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["contact", "account"])
async def test_a_campaign_takes_a_whole_list(client, monkeypatch, kind):
    """A contact list adds the people named; an account list adds everyone at those companies with
    an address. Either way the campaign remembers which list it came from."""
    from nexus.models.engagement import EngagementCampaign, MailboxConnection

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    token = await signup(client, slug=f"lmcamp{kind}", email=f"o@lmcamp{kind}.io", company="C")
    book = await _book(token)
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        box = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                email=f"o@lmcamp{kind}.io", status="connected")
        ts.add(box)
        await ts.flush()
        mailbox_id = box.id
    if kind == "contact":
        made = await _create(client, token, "People", "contact", contact_ids=[book["ava"]])
        expected = {book["ava"]}
    else:
        made = await _create(client, token, "Companies", "account",
                             account_ids=[book["acme"], book["beta"]])
        expected = {book["ava"], book["ian"]}  # Ned has no address; Beta has nobody else

    steps = [{"channel": "email", "angle": "Open on what they do"}]
    created = await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "From a list", "mailbox_id": mailbox_id, "steps": steps})
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    added = await client.post(f"/api/engagement/campaigns/{campaign_id}/contacts",
                              headers=auth(token), json={"list_id": made["id"]})
    assert added.status_code == 200, added.text
    assert set(added.json()["added"]) == expected
    async with tenant_session(me.tenant_id) as ts:
        assert (await ts.get(EngagementCampaign, campaign_id)).source_list_id == made["id"]
