# tests/test_admin_mfa_reset.py
"""Admin MFA reset — the account-recovery path.

A customer who loses their authenticator AND their recovery codes is otherwise locked out
permanently, so "contact support" has to mean support can actually act. That also makes it an
abusable action: it removes a security factor from someone else's account. Hence platform-admin
only, and audited.
"""
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def _platform_admin(client, monkeypatch, slug: str):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


async def _enrol_totp(client, token: str) -> None:
    r = await client.post("/api/auth/mfa/enroll", headers=auth(token), json={"method": "totp"})
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]

    from nexus.auth.mfa import totp_code

    code = totp_code(secret)
    confirm = await client.post(
        "/api/auth/mfa/confirm", headers=auth(token), json={"code": code}
    )
    assert confirm.status_code == 200, confirm.text


async def test_admin_can_clear_a_locked_out_users_mfa(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import User
    from nexus.models.mfa import UserMFA

    admin = await _platform_admin(client, monkeypatch, "amr1")
    victim = await signup(client, slug="amr1b", email="stuck@amr1b.com", company="AMR1B")
    await _enrol_totp(client, victim)

    async with get_sessionmaker()() as s:
        uid = (await s.scalars(select(User.id).where(User.email == "stuck@amr1b.com"))).first()
        assert (await s.scalars(select(UserMFA).where(UserMFA.user_id == uid))).first() is not None

    r = await client.delete("/api/admin/users/stuck@amr1b.com/mfa", headers=auth(admin))
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True

    async with get_sessionmaker()() as s:
        assert (await s.scalars(select(UserMFA).where(UserMFA.user_id == uid))).first() is None


async def test_reset_lets_the_user_log_in_single_step_again(client, monkeypatch):
    """The whole point: after a reset the account is reachable without the lost factor."""
    admin = await _platform_admin(client, monkeypatch, "amr2")
    await signup(client, slug="amr2b", email="back@amr2b.com", company="AMR2B")
    victim = await signup(client, slug="amr2c", email="back2@amr2c.com", company="AMR2C")
    await _enrol_totp(client, victim)

    # With MFA active, login is two-step and does not hand out an access token.
    gated = await client.post(
        "/api/auth/login", json={"email": "back2@amr2c.com", "password": "password123"}
    )
    assert "access_token" not in gated.json()

    await client.delete("/api/admin/users/back2@amr2c.com/mfa", headers=auth(admin))

    restored = await client.post(
        "/api/auth/login", json={"email": "back2@amr2c.com", "password": "password123"}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["access_token"]


async def test_a_workspace_owner_cannot_reset_anyone_else_mfa(client):
    """Removing a factor from another account is a platform power, not a tenant one."""
    owner = await signup(client, slug="amr3", email="o@amr3.com", company="AMR3")
    r = await client.delete("/api/admin/users/o@amr3.com/mfa", headers=auth(owner))
    assert r.status_code in (401, 403)


async def test_reset_is_a_noop_when_no_mfa_is_enrolled(client, monkeypatch):
    admin = await _platform_admin(client, monkeypatch, "amr4")
    await signup(client, slug="amr4b", email="plain@amr4b.com", company="AMR4B")
    r = await client.delete("/api/admin/users/plain@amr4b.com/mfa", headers=auth(admin))
    assert r.status_code == 200
    assert r.json()["cleared"] is False


async def test_unknown_user_is_rejected(client, monkeypatch):
    admin = await _platform_admin(client, monkeypatch, "amr5")
    r = await client.delete("/api/admin/users/nobody@nowhere.com/mfa", headers=auth(admin))
    assert r.status_code == 404


async def test_the_reset_is_audited_with_before_and_after(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog

    admin = await _platform_admin(client, monkeypatch, "amr6")
    victim = await signup(client, slug="amr6b", email="aud@amr6b.com", company="AMR6B")
    await _enrol_totp(client, victim)
    await client.delete("/api/admin/users/aud@amr6b.com/mfa", headers=auth(admin))

    async with get_sessionmaker()() as s:
        rows = list(
            (
                await s.scalars(
                    select(BillingAuditLog).where(BillingAuditLog.action == "user.mfa_reset")
                )
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].target == "aud@amr6b.com"
    assert "totp" in rows[0].before["methods"]
    assert rows[0].after["methods"] == []
