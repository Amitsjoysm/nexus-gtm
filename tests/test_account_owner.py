"""An account has an owner: whoever adds it, claimable, releasable, and reassignable by a manager.

Accounts had no owner at all, so "only alerts for MY accounts" had no source of truth — found when
an SDR could not route their own alerts. Decided with the product owner: add an owner.

* Whoever ADDS an account owns it: creating one, adding a lookalike, adding a company through a
  sourced person, importing a CSV. Automated sources (ICP discovery, CRM sync) leave it unowned,
  because nobody chose those rows.
* A rep can claim an unowned account or release their own. They cannot take somebody else's or
  hand one to a colleague — that is a manager's call.
* A manager and up can reassign to any member of the workspace, or clear the owner.
* NULL is unowned, not "everyone's".
"""
from __future__ import annotations

from nexus.core.security import decode_access_token
from nexus.models.account import Account
from tests.conftest import auth, signup, tenant_session


async def _member(client, owner_token: str, email: str, role: str) -> str:
    r = await client.post("/api/workspace/members", headers=auth(owner_token), json={
        "email": email, "full_name": email.split("@")[0].title(), "password": "password123",
        "role": role})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _uid(token: str) -> str:
    return (decode_access_token(token) or {})["sub"]


def _tid(token: str) -> str:
    return (decode_access_token(token) or {})["tid"]


async def _unowned_account(token: str, name: str = "Unowned Co") -> str:
    tid = _tid(token)
    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name=name, domain=f"{name.split()[0].lower()}.example")
        ts.add(acct)
        await ts.flush()
        return acct.id


def _owner_url(account_id: str) -> str:
    return f"/api/accounts/{account_id}/owner"


# ---- who owns an account from the start ------------------------------------------------------------

async def test_whoever_creates_an_account_owns_it(client):
    owner = await signup(client, slug="ow1", email="o@ow1.x", company="OW1")
    r = await client.post("/api/accounts", headers=auth(owner), json={"name": "Acme", "domain": "acme.ow1"})
    assert r.status_code == 201, r.text
    assert r.json()["owner_user_id"] == _uid(owner)
    assert r.json()["owner_name"]


async def test_adding_a_lookalike_or_a_sourced_persons_company_owns_it(client):
    owner = await signup(client, slug="ow2", email="o@ow2.x", company="OW2")
    rep = await _member(client, owner, "rep@ow2.x", "rep")

    lk = await client.post("/api/accounts/from-lookalike", headers=auth(rep),
                           json={"name": "Globex", "domain": "globex.ow2"})
    assert lk.status_code == 201, lk.text
    assert lk.json()["owner_user_id"] == _uid(rep)

    person = await client.post("/api/accounts/contacts/from-lookalike", headers=auth(rep), json={
        "full_name": "Jane Peer", "linkedin_url": "https://www.linkedin.com/in/jane-ow2",
        "new_account_name": "Initech", "new_account_domain": "initech.ow2"})
    assert person.status_code == 201, person.text
    acct = (await client.get(f"/api/accounts/{person.json()['account_id']}", headers=auth(rep))).json()
    assert acct["owner_user_id"] == _uid(rep)


async def test_a_csv_import_is_owned_by_the_person_who_imported_it():
    from nexus.imports.csv_ingest import import_accounts_csv

    tid_owner = "u" * 32
    from tests.conftest import make_tenant

    tid = await make_tenant(slug="ow3", name="OW3")
    async with tenant_session(tid) as ts:
        await import_accounts_csv(
            ts, content=b"Company,Website\nHooli,hooli.ow3\n",
            mapping={"Company": "name", "Website": "domain"}, owner_user_id=tid_owner,
        )
        rows = await ts.list(Account)
    assert [a.owner_user_id for a in rows] == [tid_owner]


async def test_a_contacts_import_owns_the_accounts_it_creates_and_no_others():
    """A contact whose company is not in the book creates the account, and the importer owns it. A
    company already in the book keeps the owner it had: importing people into it is not a claim."""
    from nexus.imports.csv_ingest import import_contacts_csv
    from tests.conftest import make_tenant

    importer = "i" * 32
    tid = await make_tenant(slug="ow3c", name="OW3C")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Acme", domain="acme.com"))
        await ts.flush()
        await import_contacts_csv(
            ts,
            content=(b"name,email,company_domain\n"
                     b"Ann Lee,ann@acme.com,acme.com\n"
                     b"Bo Chen,bo@newco.com,newco.com\n"),
            mapping={"name": "full_name", "email": "email", "company_domain": "account_domain"},
            owner_user_id=importer,
        )
        owners = {a.domain: a.owner_user_id for a in await ts.list(Account)}
    assert owners == {"acme.com": None, "newco.com": importer}


# ---- claiming and releasing -------------------------------------------------------------------------

async def test_a_rep_can_claim_an_unowned_account(client):
    owner = await signup(client, slug="ow4", email="o@ow4.x", company="OW4")
    rep = await _member(client, owner, "rep@ow4.x", "rep")
    aid = await _unowned_account(owner)

    r = await client.put(_owner_url(aid), headers=auth(rep), json={"user_id": _uid(rep)})

    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] == _uid(rep)


async def test_a_rep_cannot_take_an_account_someone_else_owns(client):
    owner = await signup(client, slug="ow5", email="o@ow5.x", company="OW5")
    rep = await _member(client, owner, "rep@ow5.x", "rep")
    aid = (await client.post("/api/accounts", headers=auth(owner),
                             json={"name": "Taken", "domain": "taken.ow5"})).json()["id"]

    r = await client.put(_owner_url(aid), headers=auth(rep), json={"user_id": _uid(rep)})
    assert r.status_code == 403, r.text


async def test_a_rep_can_release_their_own_account(client):
    owner = await signup(client, slug="ow6", email="o@ow6.x", company="OW6")
    rep = await _member(client, owner, "rep@ow6.x", "rep")
    aid = (await client.post("/api/accounts", headers=auth(rep),
                             json={"name": "Mine", "domain": "mine.ow6"})).json()["id"]

    r = await client.put(_owner_url(aid), headers=auth(rep), json={"user_id": None})

    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] is None


async def test_a_rep_cannot_release_or_hand_over_an_account_that_is_not_theirs(client):
    owner = await signup(client, slug="ow7", email="o@ow7.x", company="OW7")
    rep = await _member(client, owner, "rep@ow7.x", "rep")
    other = await _member(client, owner, "other@ow7.x", "rep")
    owned = (await client.post("/api/accounts", headers=auth(owner),
                               json={"name": "Owned", "domain": "owned.ow7"})).json()["id"]
    unowned = await _unowned_account(owner)

    assert (await client.put(_owner_url(owned), headers=auth(rep),
                             json={"user_id": None})).status_code == 403
    # Claiming is for yourself. Handing an unowned account to a colleague is a manager's call.
    assert (await client.put(_owner_url(unowned), headers=auth(rep),
                             json={"user_id": _uid(other)})).status_code == 403


# ---- reassigning --------------------------------------------------------------------------------

async def test_a_manager_can_reassign_to_any_member_or_clear_it(client):
    owner = await signup(client, slug="ow8", email="o@ow8.x", company="OW8")
    manager = await _member(client, owner, "mgr@ow8.x", "manager")
    rep = await _member(client, owner, "rep@ow8.x", "rep")
    aid = (await client.post("/api/accounts", headers=auth(owner),
                             json={"name": "Moving", "domain": "moving.ow8"})).json()["id"]

    r = await client.put(_owner_url(aid), headers=auth(manager), json={"user_id": _uid(rep)})
    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] == _uid(rep)

    r = await client.put(_owner_url(aid), headers=auth(manager), json={"user_id": None})
    assert r.status_code == 200 and r.json()["owner_user_id"] is None


async def test_an_owner_must_be_a_member_of_this_workspace(client):
    a = await signup(client, slug="ow9a", email="o@ow9a.x", company="OW9A")
    b = await signup(client, slug="ow9b", email="o@ow9b.x", company="OW9B")
    aid = await _unowned_account(a)

    assert (await client.put(_owner_url(aid), headers=auth(a),
                             json={"user_id": _uid(b)})).status_code == 422
    assert (await client.put(_owner_url(aid), headers=auth(a),
                             json={"user_id": "nobody"})).status_code == 422


async def test_owner_changes_never_reach_another_workspace(client):
    a = await signup(client, slug="ow10a", email="o@ow10a.x", company="OW10A")
    b = await signup(client, slug="ow10b", email="o@ow10b.x", company="OW10B")
    theirs = await _unowned_account(b)
    r = await client.put(_owner_url(theirs), headers=auth(a), json={"user_id": _uid(a)})
    assert r.status_code == 404


# ---- showing it ---------------------------------------------------------------------------------

async def test_the_account_list_names_each_owner(client):
    owner = await signup(client, slug="ow11", email="o@ow11.x", company="OW11")
    owned = (await client.post("/api/accounts", headers=auth(owner),
                               json={"name": "Named", "domain": "named.ow11"})).json()["id"]
    unowned = await _unowned_account(owner)

    rows = {a["id"]: a for a in (await client.get("/api/accounts", headers=auth(owner))).json()}

    assert rows[owned]["owner_name"] == "Rep"          # the signup helper's full name
    assert rows[unowned]["owner_user_id"] is None and rows[unowned]["owner_name"] is None


async def test_a_manager_can_list_members_to_assign_and_a_rep_cannot(client):
    owner = await signup(client, slug="ow12", email="o@ow12.x", company="OW12")
    manager = await _member(client, owner, "mgr@ow12.x", "manager")
    rep = await _member(client, owner, "rep@ow12.x", "rep")

    r = await client.get("/api/workspace/members/directory", headers=auth(manager))
    assert r.status_code == 200, r.text
    ids = {m["user_id"] for m in r.json()}
    assert {_uid(owner), _uid(manager), _uid(rep)} <= ids
    assert all({"user_id", "full_name", "role"} <= set(m) for m in r.json())

    assert (await client.get("/api/workspace/members/directory", headers=auth(rep))).status_code == 403


# ---- keeping it through merges and hand-overs -----------------------------------------------------

async def test_merging_fills_a_blank_owner_and_never_overwrites_one():
    from nexus.accounts.merge import merge_accounts
    from tests.conftest import make_tenant

    tid = await make_tenant(slug="ow13", name="OW13")
    keeper, other = "k" * 32, "o" * 32
    async with tenant_session(tid) as ts:
        blank_winner = Account(tenant_id=tid, name="A", domain="a.ow13")
        owned_loser = Account(tenant_id=tid, name="A dup", domain="a2.ow13", owner_user_id=other)
        owned_winner = Account(tenant_id=tid, name="B", domain="b.ow13", owner_user_id=keeper)
        other_loser = Account(tenant_id=tid, name="B dup", domain="b2.ow13", owner_user_id=other)
        ts.add_all([blank_winner, owned_loser, owned_winner, other_loser])
        await ts.flush()

        await merge_accounts(ts, winner_id=blank_winner.id, loser_id=owned_loser.id)
        await merge_accounts(ts, winner_id=owned_winner.id, loser_id=other_loser.id)

        assert blank_winner.owner_user_id == other
        assert owned_winner.owner_user_id == keeper


async def test_handing_over_someones_work_moves_the_accounts_they_own(client):
    owner = await signup(client, slug="ow14", email="o@ow14.x", company="OW14")
    leaver = await _member(client, owner, "leaver@ow14.x", "rep")
    successor = await _member(client, owner, "next@ow14.x", "rep")
    aid = (await client.post("/api/accounts", headers=auth(leaver),
                             json={"name": "Book", "domain": "book.ow14"})).json()["id"]

    r = await client.post("/api/accounts/transfer-ownership", headers=auth(owner),
                          json={"from_user_id": _uid(leaver), "to_user_id": _uid(successor)})

    assert r.status_code == 200, r.text
    assert r.json()["accounts_moved"] == 1
    acct = (await client.get(f"/api/accounts/{aid}", headers=auth(owner))).json()
    assert acct["owner_user_id"] == _uid(successor)
