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
