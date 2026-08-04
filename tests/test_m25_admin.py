# tests/test_m25_admin.py
"""M25's remainder: suspend/reactivate, merge duplicates, transfer ownership.

Each of these existed only as deletion before. Deleting a user destroys the audit trail of what
they did; deleting a duplicate account orphans the signals and tasks that explain why somebody was
contacted. All three are now reversible operations that keep the history.
"""
from __future__ import annotations

from tests.conftest import auth, make_tenant, signup, tenant_session


async def _platform(client, monkeypatch, *, slug, email):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


# ---- suspension --------------------------------------------------------------------------------

async def test_a_suspended_user_cannot_log_in(client, monkeypatch):
    """The whole control. A suspension that does not stop login is decorative."""
    token = await _platform(client, monkeypatch, slug="m25a", email="boss@m25a.com")
    await signup(client, slug="m25a2", email="rep@m25a.com", company="M25A2")

    assert (await client.post("/api/admin/users/rep@m25a.com/suspend",
                              headers=auth(token), json={"reason": "left the company"})
            ).status_code == 200

    r = await client.post("/api/auth/login",
                          json={"email": "rep@m25a.com", "password": "password123"})
    assert r.status_code == 403
    assert "suspended" in r.json()["detail"].lower()


async def test_reactivating_restores_access(client, monkeypatch):
    token = await _platform(client, monkeypatch, slug="m25b", email="boss@m25b.com")
    await signup(client, slug="m25b2", email="rep@m25b.com", company="M25B2")

    await client.post("/api/admin/users/rep@m25b.com/suspend", headers=auth(token), json={})
    await client.post("/api/admin/users/rep@m25b.com/reactivate", headers=auth(token), json={})

    r = await client.post("/api/auth/login",
                          json={"email": "rep@m25b.com", "password": "password123"})
    assert r.status_code == 200, "reactivation must actually restore login"


async def test_suspension_is_idempotent(client, monkeypatch):
    """A double-clicked button is a success, not a confusing error."""
    token = await _platform(client, monkeypatch, slug="m25c", email="boss@m25c.com")
    await signup(client, slug="m25c2", email="rep@m25c.com", company="M25C2")
    for _ in range(2):
        r = await client.post("/api/admin/users/rep@m25c.com/suspend",
                              headers=auth(token), json={})
        assert r.status_code == 200
        assert r.json()["suspended"] is True


async def test_a_wrong_password_on_a_suspended_account_still_says_invalid_credentials(
    client, monkeypatch
):
    """Suspension is checked AFTER the password, so this endpoint is not an account-existence
    oracle — an attacker must not learn which addresses are real from the difference."""
    token = await _platform(client, monkeypatch, slug="m25d", email="boss@m25d.com")
    await signup(client, slug="m25d2", email="rep@m25d.com", company="M25D2")
    await client.post("/api/admin/users/rep@m25d.com/suspend", headers=auth(token), json={})

    r = await client.post("/api/auth/login",
                          json={"email": "rep@m25d.com", "password": "wrong-password"})
    assert r.status_code == 401, "a bad password must not reveal that the account is suspended"


async def test_a_tenant_owner_cannot_suspend_anyone(client):
    token = await signup(client, slug="m25e", email="o@m25e.com", company="M25E")
    r = await client.post("/api/admin/users/someone@x.com/suspend", headers=auth(token), json={})
    assert r.status_code in (401, 403)


# ---- merge -------------------------------------------------------------------------------------

async def test_merging_moves_the_timeline_and_archives_the_loser():
    """The duplicates already in a workspace, cleaned up without losing the evidence."""
    from datetime import datetime, timezone

    from nexus.accounts.merge import merge_accounts
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug="mg1", name="MG1")
    async with tenant_session(tid) as ts:
        winner = Account(name="Acme", domain="acme.com")
        loser = Account(name="Acme Inc", domain="www.acme.com")
        ts.add(winner)
        ts.add(loser)
        await ts.flush()
        ts.add(SignalEvent(
            account_id=loser.id, kind="funding", source="test", title="Acme raises",
            dedupe_key="mg:1", strength=0.9, occurred_at=datetime.now(timezone.utc),
        ))
        await ts.flush()

        report = await merge_accounts(ts, winner_id=winner.id, loser_id=loser.id)

        moved = await ts.list(SignalEvent, SignalEvent.account_id == winner.id)
        assert len(moved) == 1, "the signal must follow the surviving account"
        assert report.moved.get("SignalEvent") == 1
        assert loser.archived_at is not None, "archived, never deleted"
        assert loser.custom_fields.get("merged_into") == winner.id


async def test_a_merge_fills_blanks_but_never_overwrites():
    """A merge must not undo a rep's correction with data from the row being discarded."""
    from nexus.accounts.merge import merge_accounts
    from nexus.models.account import Account

    tid = await make_tenant(slug="mg2", name="MG2")
    async with tenant_session(tid) as ts:
        winner = Account(name="Acme", domain="acme.com", industry="Fintech")
        loser = Account(name="Acme", domain="acme.io", industry="Logistics", country="US")
        ts.add(winner)
        ts.add(loser)
        await ts.flush()

        await merge_accounts(ts, winner_id=winner.id, loser_id=loser.id)

        assert winner.industry == "Fintech", "the winner's value survives"
        assert winner.country == "US", "but a blank is filled"


async def test_merging_an_account_into_itself_is_refused():
    from nexus.accounts.merge import merge_accounts
    from nexus.models.account import Account

    tid = await make_tenant(slug="mg3", name="MG3")
    async with tenant_session(tid) as ts:
        a = Account(name="Acme", domain="acme.com")
        ts.add(a)
        await ts.flush()
        try:
            await merge_accounts(ts, winner_id=a.id, loser_id=a.id)
        except ValueError:
            return
        raise AssertionError("merging an account into itself must be refused")


# ---- ownership transfer ---------------------------------------------------------------------------

async def test_transfer_moves_open_work_but_not_completed_history():
    """Reassigning finished tasks would rewrite who did what — that is the audit trail, not a
    queue."""
    from nexus.accounts.merge import transfer_ownership
    from nexus.models.account import Account
    from nexus.models.workflow import InboxTask

    tid = await make_tenant(slug="tr1", name="TR1")
    async with tenant_session(tid) as ts:
        account = Account(name="Acme", domain="acme.com")
        ts.add(account)
        await ts.flush()
        ts.add(InboxTask(account_id=account.id, owner_user_id="leaver", status="open",
                         title="Call Acme"))
        ts.add(InboxTask(account_id=account.id, owner_user_id="leaver", status="done",
                         title="Emailed Acme"))
        await ts.flush()

        result = await transfer_ownership(ts, from_user_id="leaver", to_user_id="stayer")
        assert result["moved"] == 1

        still_theirs = await ts.list(InboxTask, InboxTask.owner_user_id == "leaver")
        assert len(still_theirs) == 1
        assert still_theirs[0].status == "done"


async def test_transferring_to_the_same_person_is_a_no_op():
    from nexus.accounts.merge import transfer_ownership

    tid = await make_tenant(slug="tr2", name="TR2")
    async with tenant_session(tid) as ts:
        assert (await transfer_ownership(ts, from_user_id="x", to_user_id="x"))["moved"] == 0
