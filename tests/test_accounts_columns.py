"""Accounts list: Fit score from the latest AccountScore, and archive (soft-remove) hides the
account from the working list. Plus the contacts list carries LinkedIn."""
from __future__ import annotations

from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore
from tests.conftest import auth, signup, tenant_session


async def test_accounts_list_includes_fit_score(client):
    token = await signup(client, slug="acol", email="o@acol.x", company="Acol")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Brex", domain="brex.com")
        ts.add(acc)
        await ts.flush()
        ts.add(AccountScore(tenant_id=tid, account_id=acc.id, composite=83))
        await ts.flush()

    rows = (await client.get("/api/accounts", headers=auth(token))).json()
    assert rows[0]["fit_score"] == 83
    assert rows[0]["source"] is None  # passthrough field present


async def test_archive_hides_account_from_list_and_unarchive_restores(client):
    token = await signup(client, slug="arch", email="o@arch.x", company="Arch")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Junk Co", domain="junk.co")
        ts.add(acc)
        await ts.flush()
        aid = acc.id

    assert len((await client.get("/api/accounts", headers=auth(token))).json()) == 1
    r = await client.post(f"/api/accounts/{aid}/archive", headers=auth(token))
    assert r.status_code == 200
    assert (await client.get("/api/accounts", headers=auth(token))).json() == []  # hidden
    await client.post(f"/api/accounts/{aid}/unarchive", headers=auth(token))
    assert len((await client.get("/api/accounts", headers=auth(token))).json()) == 1  # back


async def test_add_from_lookalike_unarchives_existing(client):
    """Re-adding a company the rep previously removed must bring it back into the working list,
    not silently return a still-hidden row."""
    token = await signup(client, slug="lka", email="o@lka.x", company="Lka")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Ramp", domain="ramp.com")
        ts.add(acc)
        await ts.flush()
        aid = acc.id

    await client.post(f"/api/accounts/{aid}/archive", headers=auth(token))
    assert (await client.get("/api/accounts", headers=auth(token))).json() == []  # hidden

    r = await client.post(
        "/api/accounts/from-lookalike", headers=auth(token),
        json={"name": "Ramp", "domain": "ramp.com"},
    )
    assert r.status_code in (200, 201)
    assert r.json()["id"] == aid  # deduped to the existing account
    rows = (await client.get("/api/accounts", headers=auth(token))).json()
    assert [a["id"] for a in rows] == [aid]  # un-archived -> visible again


async def test_accounts_list_is_newest_first(client):
    """A just-added account lands at the top of the list (deterministic newest-first order)."""
    from datetime import timedelta

    from nexus.core.db import utcnow

    token = await signup(client, slug="ord", email="o@ord.x", company="Ord")
    tid = decode_access_token(token)["tid"]
    base = utcnow()
    async with tenant_session(tid) as ts:
        for i, name in enumerate(("First Co", "Second Co", "Third Co")):
            ts.add(Account(tenant_id=tid, name=name, domain=f"c{i}.co",
                           created_at=base + timedelta(minutes=i)))
        await ts.flush()

    rows = (await client.get("/api/accounts", headers=auth(token))).json()
    assert [a["name"] for a in rows] == ["Third Co", "Second Co", "First Co"]


async def test_contacts_list_is_tenant_isolated(client):
    """The workspace Contacts list must never surface another tenant's people (the query is the
    only isolation layer — RLS can be bypassed by the DB role)."""
    t1 = await signup(client, slug="iso1", email="o@iso1.x", company="Iso One")
    t2 = await signup(client, slug="iso2", email="o@iso2.x", company="Iso Two")
    tid1 = decode_access_token(t1)["tid"]
    tid2 = decode_access_token(t2)["tid"]
    async with tenant_session(tid1) as ts:
        a = Account(tenant_id=tid1, name="Acme One", domain="one.co")
        ts.add(a)
        await ts.flush()
        ts.add(Contact(tenant_id=tid1, account_id=a.id, full_name="Alice One", email="a@one.co"))
        await ts.flush()
    async with tenant_session(tid2) as ts:
        b = Account(tenant_id=tid2, name="Acme Two", domain="two.co")
        ts.add(b)
        await ts.flush()
        ts.add(Contact(tenant_id=tid2, account_id=b.id, full_name="Bob Two", email="b@two.co"))
        await ts.flush()

    r1 = (await client.get("/api/contacts", headers=auth(t1))).json()
    r2 = (await client.get("/api/contacts", headers=auth(t2))).json()
    assert [c["full_name"] for c in r1] == ["Alice One"]   # tenant 1 sees only its own
    assert [c["full_name"] for c in r2] == ["Bob Two"]      # tenant 2 sees only its own


async def test_contacts_list_carries_linkedin(client):
    token = await signup(client, slug="cli", email="o@cli.x", company="Cli")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        ts.add(Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                       title="VP Sales", linkedin_url="https://linkedin.com/in/jane"))
        await ts.flush()

    rows = (await client.get("/api/contacts", headers=auth(token))).json()
    assert rows[0]["linkedin_url"] == "https://linkedin.com/in/jane"
    assert rows[0]["account_name"] == "Acme"
