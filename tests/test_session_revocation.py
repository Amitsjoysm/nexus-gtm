# tests/test_session_revocation.py
"""Suspending, resetting or demoting a user must end the sessions they already hold.

`get_principal` decoded the JWT and never touched the database, and access tokens carry no `jti`.
For the full `access_token_ttl_min` after the event — 60 minutes by default — a suspended user
kept working, a password reset did not evict an attacker, and an admin demoted to rep kept admin
rights. This is the first question in every enterprise security review and it had no answer.

The mechanism is a per-user counter stamped into the token. Bumping it invalidates every token
issued before the bump, with no denylist to store or expire.

Compatibility is the load-bearing part: a token carrying NO `tv` claim is accepted. Those are the
tokens issued by the previous release, and refusing them would log out every active user the
moment this deploys.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from tests.conftest import make_tenant, tenant_session


async def _user(tid: str, email: str = "u@example.com"):
    from nexus.core.db import get_sessionmaker
    from nexus.core.security import hash_password
    from nexus.models.identity import Membership, User, Workspace

    async with get_sessionmaker()() as db:
        user = User(email=email, full_name="U", password_hash=hash_password("x"))
        db.add(user)
        await db.flush()
        ws = Workspace(tenant_id=tid, name="W")
        db.add(ws)
        await db.flush()
        db.add(Membership(tenant_id=tid, user_id=user.id, workspace_id=ws.id, role="owner"))
        await db.commit()
        return user.id


def _creds(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture(autouse=True)
def _clear_cache():
    from nexus.api.deps import clear_session_version_cache

    clear_session_version_cache()
    yield
    clear_session_version_cache()


async def test_a_token_stops_working_once_the_user_is_revoked():
    from nexus.api.deps import clear_session_version_cache, get_principal
    from nexus.auth.sessions import revoke_user_sessions
    from nexus.core.db import get_sessionmaker
    from nexus.core.security import create_access_token

    tid = await make_tenant()
    uid = await _user(tid)
    token = create_access_token(user_id=uid, tenant_id=tid, role="owner", token_version=0)

    principal = await get_principal(_creds(token))
    assert principal.user_id == uid          # works before revocation

    async with get_sessionmaker()() as db:
        await revoke_user_sessions(db, uid)
        await db.commit()
    clear_session_version_cache()

    with pytest.raises(HTTPException) as exc:
        await get_principal(_creds(token))
    assert exc.value.status_code == 401


async def test_a_token_issued_after_the_revocation_works():
    from nexus.api.deps import clear_session_version_cache, get_principal
    from nexus.auth.sessions import current_token_version, revoke_user_sessions
    from nexus.core.db import get_sessionmaker
    from nexus.core.security import create_access_token

    tid = await make_tenant()
    uid = await _user(tid)

    async with get_sessionmaker()() as db:
        await revoke_user_sessions(db, uid)
        await db.commit()
    clear_session_version_cache()

    async with get_sessionmaker()() as db:
        version = await current_token_version(db, uid)
    fresh = create_access_token(
        user_id=uid, tenant_id=tid, role="owner", token_version=version
    )
    assert (await get_principal(_creds(fresh))).user_id == uid


async def test_a_token_from_the_previous_release_is_still_accepted():
    """No `tv` claim at all. Refusing these would log out every active user on deploy."""
    from datetime import timedelta

    from jose import jwt

    from nexus.api.deps import get_principal
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow

    tid = await make_tenant()
    uid = await _user(tid)
    s = get_settings()
    now = utcnow()
    legacy = jwt.encode(
        {
            "sub": uid, "tid": tid, "role": "owner",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=30)).timestamp()),
        },
        s.secret_key,
        algorithm=s.jwt_algorithm,
    )
    assert (await get_principal(_creds(legacy))).user_id == uid


def test_every_deprovisioning_path_revokes_sessions():
    """Structural, because the alternative is remembering.

    Each of these is a moment where access is supposed to stop. A path that changes the row and
    forgets to bump the counter leaves the old token working for the rest of its TTL — which is
    exactly the bug this whole mechanism exists to close, reintroduced one endpoint at a time.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    required = {
        "nexus/api/routers/admin_users.py": "suspend / reactivate",
        "nexus/api/routers/workspace.py": "role change / member removal",
        "nexus/auth/password_reset.py": "password reset",
    }
    missing = [
        f"{rel} ({why})"
        for rel, why in required.items()
        if "revoke_user_sessions" not in (root / rel).read_text(encoding="utf-8")
    ]
    assert not missing, "these deprovisioning paths leave existing tokens valid: " + ", ".join(missing)
