# tests/test_billing_platform_admins.py
"""Who may operate the platform, and how that is granted.

Platform admin is not a tenant role. The `whoami` endpoint exists so the SPA can route on the
real answer instead of guessing from workspace role — guarding the staff console on
`minRole="owner"` let any workspace owner load its shell.
"""
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def _as_admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


# ---- whoami -----------------------------------------------------------------------------

async def test_whoami_says_no_for_a_workspace_owner(client):
    """A workspace owner is emphatically not a platform admin, and must be told so plainly
    rather than with a 403 the SPA cannot distinguish from a bug."""
    token = await signup(client, slug="wai1", email="o@wai1.com", company="WAI1")
    r = await client.get("/api/admin/billing/whoami", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["is_platform_admin"] is False


async def test_whoami_says_yes_for_a_platform_admin(client, monkeypatch):
    token = await _as_admin(client, monkeypatch, "wai2")
    r = await client.get("/api/admin/billing/whoami", headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["is_platform_admin"] is True
    assert body["email"] == "boss@nexus.com"


async def test_whoami_requires_authentication(client):
    r = await client.get("/api/admin/billing/whoami")
    # 401/403 both fine, and this route is deliberately NOT hidden: it answers "am I an admin?"
    # with false for ordinary users, which is how the SPA decides whether to show admin navigation
    # at all. A 404 here would break that, and its existence is not the secret -- what it guards is.
    assert r.status_code in (401, 403)


# ---- granting ---------------------------------------------------------------------------

async def test_an_admin_can_create_another_admin(client, monkeypatch):
    token = await _as_admin(client, monkeypatch, "pa1")
    r = await client.post("/api/admin/billing/admins", headers=auth(token),
                          json={"email": "colleague@nexus.com", "platform_role": "superadmin"})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True

    listed = await client.get("/api/admin/billing/admins", headers=auth(token))
    assert any(a["email"] == "colleague@nexus.com" for a in listed.json())


async def test_the_new_admin_actually_gains_access(client, monkeypatch):
    """A grant that does not confer access is decoration."""
    from nexus.core.config import get_settings

    token = await _as_admin(client, monkeypatch, "pa2")
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "newbie@nexus.com", "platform_role": "superadmin"})

    # Drop the env allowlist so ONLY the database grant can be doing the work.
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "")
    newbie = await signup(client, slug="pa2b", email="newbie@nexus.com", company="PA2B")
    r = await client.get("/api/admin/billing/plans", headers=auth(newbie))
    assert r.status_code == 200


async def test_a_workspace_owner_cannot_make_themselves_an_admin(client):
    """The privilege-escalation path that matters most."""
    token = await signup(client, slug="pa3", email="o@pa3.com", company="PA3")
    r = await client.post("/api/admin/billing/admins", headers=auth(token),
                          json={"email": "o@pa3.com", "platform_role": "superadmin"})
    assert r.status_code in (401, 404)


async def test_granting_rejects_bad_input(client, monkeypatch):
    token = await _as_admin(client, monkeypatch, "pa4")
    bad_email = await client.post("/api/admin/billing/admins", headers=auth(token),
                                  json={"email": "not-an-email", "platform_role": "superadmin"})
    assert bad_email.status_code == 422

    bad_role = await client.post("/api/admin/billing/admins", headers=auth(token),
                                 json={"email": "x@nexus.com", "platform_role": "god"})
    assert bad_role.status_code == 422


# ---- revoking ---------------------------------------------------------------------------

async def test_revoking_deactivates_rather_than_deletes(client, monkeypatch):
    token = await _as_admin(client, monkeypatch, "pa5")
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "a@nexus.com", "platform_role": "superadmin"})
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "b@nexus.com", "platform_role": "support"})

    r = await client.delete("/api/admin/billing/admins/b@nexus.com", headers=auth(token))
    assert r.status_code == 200

    rows = (await client.get("/api/admin/billing/admins", headers=auth(token))).json()
    revoked = next(a for a in rows if a["email"] == "b@nexus.com")
    # Still present, so the audit trail keeps pointing at a real row.
    assert revoked["active"] is False


async def test_the_last_admin_cannot_be_revoked(client, monkeypatch):
    """Locking every operator out of the billing console is not recoverable through the product."""
    token = await _as_admin(client, monkeypatch, "pa6")
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "only@nexus.com", "platform_role": "superadmin"})

    r = await client.delete("/api/admin/billing/admins/only@nexus.com", headers=auth(token))
    assert r.status_code == 409
    assert "last active" in r.text.lower()


async def test_grant_and_revoke_are_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _as_admin(client, monkeypatch, "pa7")
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "keep@nexus.com", "platform_role": "superadmin"})
    await client.post("/api/admin/billing/admins", headers=auth(token),
                      json={"email": "drop@nexus.com", "platform_role": "support"})
    await client.delete("/api/admin/billing/admins/drop@nexus.com", headers=auth(token))

    async with get_sessionmaker()() as s:
        actions = [
            r.action for r in (await s.scalars(select(BillingAuditLog))).all()
        ]
    assert "platform_admin.grant" in actions
    assert "platform_admin.revoke" in actions
