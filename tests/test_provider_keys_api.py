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


# ---- the model, and the live catalogue -----------------------------------------------------------

async def test_the_model_can_be_chosen_and_cleared(client, monkeypatch):
    """The fix for the outage that started this: `llama-3.3-70b-versatile` was withdrawn and
    changing it meant editing deploy/.env and redeploying."""
    from nexus.providers.resolver import invalidate_models, model_for

    token = await _superadmin(client, monkeypatch, slug="pm1", email="boss@pm1.com")
    r = await client.put("/api/admin/provider-keys/groq/model", headers=auth(token),
                         json={"model": "openai/gpt-oss-120b"})
    assert r.status_code == 200, r.text
    invalidate_models()
    assert await model_for("groq") == "openai/gpt-oss-120b"

    # Empty clears the override and the environment value applies again.
    await client.put("/api/admin/provider-keys/groq/model", headers=auth(token), json={"model": ""})
    invalidate_models()
    from nexus.core.config import get_settings

    assert await model_for("groq") == get_settings().groq_model


async def test_an_unknown_model_is_accepted_because_the_catalogue_is_theirs(client, monkeypatch):
    """Refusing an unlisted model would mean a withdrawn-model outage could not be fixed from
    here — which is the exact situation this endpoint exists for."""
    token = await _superadmin(client, monkeypatch, slug="pm2", email="boss@pm2.com")
    r = await client.put("/api/admin/provider-keys/groq/model", headers=auth(token),
                         json={"model": "something-new-they-just-shipped"})
    assert r.status_code == 200


async def test_setting_a_model_for_an_unknown_provider_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="pm3", email="boss@pm3.com")
    r = await client.put("/api/admin/provider-keys/pipedrive/model", headers=auth(token),
                         json={"model": "x"})
    assert r.status_code == 400


async def test_listing_models_without_a_key_says_why(client, monkeypatch):
    """"we could not ask" and "there are none" are different facts, and a bare [] conflates them."""
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="pm4", email="boss@pm4.com")
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    r = await client.get("/api/admin/provider-keys/anthropic/models", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"] == []
    assert "no usable key" in body["detail"]


# ---- the platform overview -----------------------------------------------------------------------

async def test_the_overview_reports_users_and_requests(client, monkeypatch):
    """Neither number existed anywhere: the Subscriptions tab shows plan and status,
    /billing/usage is tenant-scoped, and the user-activity endpoint answers for one person."""
    token = await _superadmin(client, monkeypatch, slug="ov1", email="boss@ov1.com")
    r = await client.get("/api/admin/billing/overview", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["users"] >= 1
    assert body["tenants"] >= 1
    assert body["requests_total"] >= 0
    # Attribution is partial by construction — only usage events carry a user_id, and background
    # work has nobody to attribute it to. The number is reported so the UI can say so.
    assert body["requests_with_a_user"] <= body["requests_total"]


async def test_a_tenant_owner_cannot_read_the_overview(client):
    token = await signup(client, slug="ov2", email="o@ov2.com", company="OV2")
    assert (await client.get("/api/admin/billing/overview",
                             headers=auth(token))).status_code == 403


async def test_the_overview_counts_usage_across_every_tenant(client, monkeypatch):
    """It first reported 0 against a database holding 18 events.

    `billing_usage_events` is tenant-scoped, so a cross-tenant aggregate on the RLS-bound app role
    returns ZERO ROWS rather than raising — the documented trap, and it reads as "nobody has used
    anything". This asserts the count survives events belonging to a tenant the caller is not.
    """
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.billing import BillingUsageEvent

    token = await _superadmin(client, monkeypatch, slug="ov3", email="boss@ov3.com")
    async with get_platform_sessionmaker()() as s:
        s.add(BillingUsageEvent(
            tenant_id="some-other-tenant", capability_id="ai.email_draft",
            quantity=1, unit="action", idempotency_key="overview-probe-1",
            occurred_at=utcnow(),
        ))
        await s.commit()

    body = (await client.get("/api/admin/billing/overview", headers=auth(token))).json()
    assert body["requests_total"] >= 1, "a cross-tenant aggregate must see other tenants' rows"
