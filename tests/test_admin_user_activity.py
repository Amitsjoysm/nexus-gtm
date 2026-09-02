# tests/test_admin_user_activity.py
"""What a platform admin can see about one user's behaviour.

The reason this endpoint is careful rather than convenient: **attribution is partial**. Only
`billing_usage_events` carries a `user_id`. Signals, agent runs, inbox tasks, calls and alerts are
tenant-scoped with no actor column, so a console that merged them into "this user's activity" would
let a support agent tell a customer that a named person did something a colleague did.

So the tests below are mostly about the seams: user-attributed actions and workspace context must
stay in separate lists, and the response must admit the gap.
"""
from __future__ import annotations

from sqlalchemy import select

from tests.conftest import auth, signup


async def _admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


async def _suspendable(client, slug: str) -> str:
    """A user OTHER than the admin, to be suspended.

    Suspending an account now revokes the sessions it already holds (nexus/auth/sessions.py), so
    an admin who suspends the account they are signed in as is immediately logged out — correctly.
    These tests used one account for both roles, so they were asserting on a request made with a
    token their own previous request had just invalidated.
    """
    await signup(client, slug=slug, email=f"{slug}@target.com", company=slug.upper())
    return f"{slug}@target.com"


async def _user_id(email: str) -> str:
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import User

    async with get_sessionmaker()() as s:
        return (await s.scalars(select(User.id).where(User.email == email))).first()


async def _usage(tenant_id: str, capability: str, user_id: str | None) -> None:
    """A metered action, with or without an actor — both shapes occur in real data."""
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tenant_id) as ts:
        ts.add(BillingUsageEvent(
            capability_id=capability, quantity=1, unit="action", source="api",
            user_id=user_id, idempotency_key=f"k-{capability}-{user_id or 'none'}",
            occurred_at=utcnow(),
        ))
        await ts.flush()


# ---- the gate ----------------------------------------------------------------------------------

async def test_a_workspace_owner_cannot_read_it(client):
    """One person's behaviour is not something a tenant role should reach."""
    token = await signup(client, slug="ua1", email="o@ua1.com", company="UA1")
    r = await client.get("/api/admin/users/o@ua1.com/activity", headers=auth(token))
    assert r.status_code in (401, 404)


async def test_it_requires_authentication(client):
    assert (await client.get("/api/admin/users/x@y.com/activity")).status_code in (401, 404)


async def test_an_unknown_user_is_404_not_an_empty_report(client, monkeypatch):
    """An empty report for a mistyped address reads as "this person did nothing"."""
    token = await _admin(client, monkeypatch, "ua2")
    r = await client.get("/api/admin/users/nobody@nowhere.com/activity", headers=auth(token))
    assert r.status_code == 404


# ---- what it returns ---------------------------------------------------------------------------

async def test_it_reports_identity_and_memberships(client, monkeypatch):
    token = await _admin(client, monkeypatch, "ua3")
    r = await client.get("/api/admin/users/boss@nexus.com/activity", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "boss@nexus.com"
    assert body["suspended"] is False
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["role"] == "owner"
    assert body["memberships"][0]["slug"] == "ua3"


async def test_metered_actions_are_only_this_users(client, monkeypatch):
    """THE separation. A colleague's action must never appear as this person's."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await _admin(client, monkeypatch, "ua4")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "ua4"))).first()
    uid = await _user_id("boss@nexus.com")

    await _usage(tid, "ai.email_draft", uid)          # theirs
    await _usage(tid, "ai.research_brief", "someone-else")   # a colleague's
    await _usage(tid, "enrich.contact", None)          # unattributed

    body = (await client.get("/api/admin/users/boss@nexus.com/activity",
                             headers=auth(token))).json()
    mine = {a["capability_id"] for a in body["metered_actions"]}
    assert mine == {"ai.email_draft"}
    # ...but the workspace list still shows all three, as CONTEXT.
    assert len(body["workspace_activity"]) == 3


async def test_workspace_activity_marks_what_is_attributed(client, monkeypatch):
    """An operator has to be able to see which rows actually name a person."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    token = await _admin(client, monkeypatch, "ua5")
    async with get_sessionmaker()() as s:
        tid = (await s.scalars(select(Tenant.id).where(Tenant.slug == "ua5"))).first()
    uid = await _user_id("boss@nexus.com")
    await _usage(tid, "ai.email_draft", uid)
    await _usage(tid, "enrich.contact", None)

    body = (await client.get("/api/admin/users/boss@nexus.com/activity",
                             headers=auth(token))).json()
    flags = {a["capability_id"]: a["attributed"] for a in body["workspace_activity"]}
    assert flags["ai.email_draft"] is True
    assert flags["enrich.contact"] is False


async def test_the_response_admits_the_attribution_gap(client, monkeypatch):
    """Silence about the gap is what makes a partial trail dangerous — "no activity" would read
    as "did nothing" rather than "not recorded against a user"."""
    token = await _admin(client, monkeypatch, "ua6")
    body = (await client.get("/api/admin/users/boss@nexus.com/activity",
                             headers=auth(token))).json()
    assert "user id" in body["attribution_note"]
    assert body["metered_actions"] == []      # nothing metered yet, and that is stated honestly


async def test_admin_actions_against_the_account_are_shown(client, monkeypatch):
    """The other half of most tickets is "why can't I log in?", and a suspension with no visible
    record is exactly the one that gets reversed by mistake."""
    token = await _admin(client, monkeypatch, "ua7")
    target = await _suspendable(client, "uatgt7")
    await client.post(f"/api/admin/users/{target}/suspend",
                      headers=auth(token), json={"reason": "ticket 42 — abuse report"})

    body = (await client.get(f"/api/admin/users/{target}/activity",
                             headers=auth(token))).json()
    assert body["suspended"] is True
    actions = {a["action"]: a for a in body["admin_actions"]}
    assert "user.suspend" in actions
    assert "ticket 42" in actions["user.suspend"]["note"]


async def test_a_suspension_reason_is_surfaced(client, monkeypatch):
    token = await _admin(client, monkeypatch, "ua8")
    target = await _suspendable(client, "uatgt8")
    await client.post(f"/api/admin/users/{target}/suspend",
                      headers=auth(token), json={"reason": "payment dispute"})
    body = (await client.get(f"/api/admin/users/{target}/activity",
                             headers=auth(token))).json()
    assert body["suspended_reason"] == "payment dispute"


async def test_the_limit_is_bounded(client, monkeypatch):
    """An admin endpoint that will happily return a million rows is a self-inflicted outage."""
    token = await _admin(client, monkeypatch, "ua9")
    r = await client.get("/api/admin/users/boss@nexus.com/activity?limit=99999",
                         headers=auth(token))
    assert r.status_code == 200
    assert len(r.json()["workspace_activity"]) <= 200
