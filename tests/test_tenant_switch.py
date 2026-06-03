# tests/test_tenant_switch.py
"""Cross-workspace switching: list memberships, switch re-issues a tenant-pinned JWT."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _add_membership(user_email: str, slug: str, name: str, role: str = "admin") -> str:
    """Provision a second tenant + workspace and bind the existing user into it."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Membership, Tenant, User, Workspace
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        tenant = Tenant(name=name, slug=slug)
        s.add(tenant)
        await s.flush()
        ws = Workspace(tenant_id=tenant.id, name=f"{name} WS")
        s.add(ws)
        await s.flush()
        user = (await s.scalars(select(User).where(User.email == user_email))).first()
        s.add(Membership(tenant_id=tenant.id, user_id=user.id, workspace_id=ws.id, role=role))
        await s.commit()
        return tenant.id


@pytest.mark.asyncio
async def test_list_tenants_returns_all_memberships(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    other_id = await _add_membership("rep@acme.com", "beta", "Beta Inc", role="manager")
    r = await client.get("/api/auth/tenants", headers=auth(token))
    assert r.status_code == 200, r.text
    slugs = {t["slug"]: t for t in r.json()}
    assert {"acme", "beta"} <= set(slugs)
    assert slugs["beta"]["role"] == "manager"
    assert slugs["acme"]["role"] == "owner"


@pytest.mark.asyncio
async def test_switch_reissues_token_for_member_tenant(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    other_id = await _add_membership("rep@acme.com", "beta", "Beta Inc", role="manager")
    r = await client.post("/api/auth/switch", json={"tenant_id": other_id}, headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == other_id
    assert body["role"] == "manager"
    # The new token must work and be pinned to the new tenant.
    r2 = await client.get("/api/auth/tenants", headers=auth(body["access_token"]))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_switch_rejects_non_member_tenant(client):
    token = await signup(client, slug="acme", email="rep@acme.com", company="Acme")
    # A tenant the user is NOT a member of.
    from tests.conftest import make_tenant

    foreign = await make_tenant("foreign", "Foreign Co")
    r = await client.post("/api/auth/switch", json={"tenant_id": foreign}, headers=auth(token))
    assert r.status_code == 403, r.text
