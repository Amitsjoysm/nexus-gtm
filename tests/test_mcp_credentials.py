# tests/test_mcp_credentials.py
"""API keys and bearer resolution for connected AI apps (spec 2026-10-13, task 3).

Every call re-reads the user's LIVE role and suspension from the database (through a 30 s cache),
so demoting or suspending someone shrinks or stops their connected apps without anyone having to
find and revoke each key."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from nexus.core.db import get_sessionmaker, utcnow
from nexus.mcp import credentials
from nexus.models.identity import Membership, User
from nexus.models.mcp import McpConnection
from tests.conftest import auth, principal_from_token, signup


@pytest.fixture(autouse=True)
def _forget():
    credentials.forget()
    yield
    credentials.forget()


async def _key(token: str, *, scopes=("read", "research", "write"), days=90):
    p = principal_from_token(token)
    async with get_sessionmaker()() as s:
        conn, secret = await credentials.create_api_key(
            s, user_id=p.user_id, tenant_id=p.tenant_id, name="laptop", scopes=list(scopes),
            expires_in_days=days)
        await s.commit()
    return conn, secret, p


async def test_a_key_resolves_to_its_owner_workspace_role_and_scopes(client):
    token = await signup(client, slug="mk1", email="o@mk1.com")
    conn, secret, p = await _key(token, scopes=("read",))
    assert secret.startswith("nxk_") and len(secret) > 40
    actor = await credentials.resolve(secret)
    assert (actor.user_id, actor.tenant_id, actor.role) == (p.user_id, p.tenant_id, "owner")
    assert actor.scopes == frozenset({"read"}) and actor.kind == "api_key"
    assert actor.connection_id == conn.id


async def test_only_a_digest_and_a_short_prefix_are_stored(client):
    token = await signup(client, slug="mk2", email="o@mk2.com")
    conn, secret, _ = await _key(token)
    async with get_sessionmaker()() as s:
        row = await s.get(McpConnection, conn.id)
    stored = " ".join(str(getattr(row, c.name)) for c in McpConnection.__table__.columns)
    assert secret not in stored
    assert row.key_prefix == secret[:12] and len(row.key_digest) == 64


async def test_a_revoked_key_stops_once_the_cache_is_cleared(client):
    token = await signup(client, slug="mk3", email="o@mk3.com")
    conn, secret, _ = await _key(token)
    await credentials.resolve(secret)
    async with get_sessionmaker()() as s:
        row = await s.get(McpConnection, conn.id)
        row.revoked_at = utcnow()
        await s.commit()
    credentials.forget(conn.id)
    with pytest.raises(credentials.CredentialError) as e:
        await credentials.resolve(secret)
    assert e.value.reason == "revoked"


async def test_an_expired_key_is_refused(client):
    token = await signup(client, slug="mk4", email="o@mk4.com")
    conn, secret, _ = await _key(token)
    async with get_sessionmaker()() as s:
        row = await s.get(McpConnection, conn.id)
        row.expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()
    with pytest.raises(credentials.CredentialError) as e:
        await credentials.resolve(secret)
    assert e.value.reason == "expired"


async def test_a_suspended_users_key_is_refused(client):
    token = await signup(client, slug="mk5", email="o@mk5.com")
    _, secret, p = await _key(token)
    async with get_sessionmaker()() as s:
        user = await s.get(User, p.user_id)
        user.suspended_at = utcnow()
        await s.commit()
    with pytest.raises(credentials.CredentialError) as e:
        await credentials.resolve(secret)
    assert e.value.reason == "suspended"


async def test_a_demoted_users_key_carries_the_new_role(client):
    owner = await signup(client, slug="mk6", email="o@mk6.com")
    r = await client.post("/api/workspace/members", headers=auth(owner), json={
        "email": "m@mk6.com", "full_name": "Manager", "password": "password123",
        "role": "manager"})
    assert r.status_code == 201, r.text
    membership_id = r.json()["membership_id"]
    login = await client.post("/api/auth/login", json={"email": "m@mk6.com",
                                                       "password": "password123"})
    manager = login.json()["access_token"]
    _, secret, _ = await _key(manager)
    assert (await credentials.resolve(secret)).role == "manager"
    r = await client.put(f"/api/workspace/members/{membership_id}/role", headers=auth(owner),
                         json={"role": "rep"})
    assert r.status_code == 200, r.text
    credentials.forget()
    assert (await credentials.resolve(secret)).role == "rep"


async def test_a_key_of_someone_removed_from_the_workspace_is_refused(client):
    token = await signup(client, slug="mk7", email="o@mk7.com")
    _, secret, p = await _key(token)
    async with get_sessionmaker()() as s:
        for m in (await s.scalars(select(Membership).where(Membership.user_id == p.user_id))).all():
            await s.delete(m)
        await s.commit()
    with pytest.raises(credentials.CredentialError) as e:
        await credentials.resolve(secret)
    assert e.value.reason == "no_membership"


@pytest.mark.parametrize("bearer", ["", "nxk_", "nxk_not-a-real-key-at-all-0000000000000",
                                    "Bearer nxk_x", "eyJhbGciOi.x.y", "nxo_" + "x" * 400])
async def test_unknown_or_malformed_bearers_are_invalid(client, bearer):
    with pytest.raises(credentials.CredentialError) as e:
        await credentials.resolve(bearer)
    assert e.value.reason == "invalid"


async def test_the_limiter_allows_the_limit_then_says_when_to_retry():
    from nexus.core import ratelimit

    ratelimit.reset_rate_limits()
    assert [await ratelimit.hit("mcp:test-conn", window=60, limit=3) for _ in range(3)] == [None] * 3
    retry = await ratelimit.hit("mcp:test-conn", window=60, limit=3)
    assert retry is not None and retry > 0
