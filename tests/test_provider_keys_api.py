# tests/test_provider_keys_api.py
"""The Control-plane surface for provider keys.

Gated on ``providers.manage``, carried only by the ``superadmin`` preset: a holder can spend money
through someone else's API key, so it is not folded into ``admins.manage``. Same argument that
keeps ``sources.manage`` separate.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_key_is_never_returned_in_any_response(client, monkeypatch):
    """There is deliberately no endpoint that reveals a stored key. A panel that can display a
    credential is one that can leak it through a screenshot or a support session."""
    token = await _superadmin(client, monkeypatch, slug="pk1", email="boss@pk1.com")
    created = await client.post("/api/admin/provider-keys", headers=auth(token),
                                json={"provider": "exa", "label": "p", "key": "sk-secret-9999"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert "sk-secret-9999" not in str(body)
    assert body["key_hint"] == "9999"

    listed = (await client.get("/api/admin/provider-keys", headers=auth(token))).json()
    assert "sk-secret-9999" not in str(listed)


async def test_a_tenant_owner_cannot_reach_it(client):
    """No tenant role grants platform power, however senior."""
    token = await signup(client, slug="pk2", email="o@pk2.com", company="PK2")
    r = await client.get("/api/admin/provider-keys", headers=auth(token))
    assert r.status_code == 403


async def test_a_request_body_cannot_set_status(client, monkeypatch):
    """`extra="forbid"` rejects it rather than quietly dropping it. An admin who could write
    `verified` by hand could mark a dead key working."""
    token = await _superadmin(client, monkeypatch, slug="pk3", email="boss@pk3.com")
    r = await client.post("/api/admin/provider-keys", headers=auth(token),
                          json={"provider": "exa", "label": "x", "key": "sk-x-11111",
                                "status": "verified"})
    assert r.status_code == 422


async def test_a_duplicate_key_is_a_409(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pk4", email="boss@pk4.com")
    body = {"provider": "brave", "label": "a", "key": "sk-dup-22222"}
    assert (await client.post("/api/admin/provider-keys", headers=auth(token),
                              json=body)).status_code == 201
    again = await client.post("/api/admin/provider-keys", headers=auth(token),
                              json={**body, "label": "b"})
    assert again.status_code == 409


async def test_an_unknown_provider_is_a_400(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pk5", email="boss@pk5.com")
    r = await client.post("/api/admin/provider-keys", headers=auth(token),
                          json={"provider": "pipedrive", "label": "x", "key": "sk-x-33333"})
    assert r.status_code == 400


async def test_preferring_a_key_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="pk6", email="boss@pk6.com")
    created = (await client.post("/api/admin/provider-keys", headers=auth(token),
                                 json={"provider": "serper", "label": "b",
                                       "key": "sk-b-44444"})).json()
    r = await client.post(f"/api/admin/provider-keys/{created['id']}/prefer", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["preferred"] is True

    async with get_platform_sessionmaker()() as s:
        actions = [row.action for row in (await s.scalars(select(BillingAuditLog))).all()]
    assert "provider_key.prefer" in actions
    assert "provider_key.create" in actions


async def test_the_audit_records_the_hint_and_never_the_key(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="pk7", email="boss@pk7.com")
    await client.post("/api/admin/provider-keys", headers=auth(token),
                      json={"provider": "github", "label": "g", "key": "ghp-audit-55555"})

    async with get_platform_sessionmaker()() as s:
        rows = list((await s.scalars(select(BillingAuditLog))).all())
    assert not any("ghp-audit-55555" in str(r.after or "") for r in rows)


async def test_an_unknown_test_depth_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pk8", email="boss@pk8.com")
    created = (await client.post("/api/admin/provider-keys", headers=auth(token),
                                 json={"provider": "exa", "label": "d",
                                       "key": "sk-d-66666"})).json()
    r = await client.post(f"/api/admin/provider-keys/{created['id']}/test?depth=deep",
                          headers=auth(token))
    assert r.status_code == 400


async def test_deleting_a_key_removes_it(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pk9", email="boss@pk9.com")
    created = (await client.post("/api/admin/provider-keys", headers=auth(token),
                                 json={"provider": "apify", "label": "gone",
                                       "key": "apify-gone-77777"})).json()
    assert (await client.delete(f"/api/admin/provider-keys/{created['id']}",
                                headers=auth(token))).status_code == 204
    listed = (await client.get("/api/admin/provider-keys?provider=apify",
                               headers=auth(token))).json()
    assert not any(r["id"] == created["id"] for r in listed)


async def test_the_supported_provider_list_is_offered(client, monkeypatch):
    """The UI needs the ids; a hardcoded frontend list would drift from the catalog."""
    token = await _superadmin(client, monkeypatch, slug="pk10", email="boss@pk10.com")
    r = await client.get("/api/admin/provider-keys/providers", headers=auth(token))
    assert r.status_code == 200, r.text
    ids = {p["id"] for p in r.json()}
    assert "groq" in ids and "exa" in ids and len(ids) == 9
