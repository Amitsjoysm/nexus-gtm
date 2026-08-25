"""Per-tenant SEP credentials, mirroring the CRM set.

The one behaviour unique to SEP: the deployment default is an explicit *stub*. Before this work
``get_sep_connector()`` returned ``OutreachConnector()`` — itself a recording stub — so an
unconfigured deployment reported every push as a success that never happened.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session


@pytest.fixture(autouse=True)
def no_sep_override():
    """Other modules install a recording stub via set_sep_connector and never clear it; an
    override outranks every tenant credential, so leaving one installed would make the
    resolution tests below pass vacuously."""
    from nexus.integrations.sep import set_sep_connector

    set_sep_connector(None)
    yield
    set_sep_connector(None)


def _fixed(status: int, body: dict):
    async def _req(method: str, path: str, request_body: dict | None = None):
        return status, body
    return _req


# ---- the default is a named stub ----------------------------------------------------------
def test_the_deployment_default_is_an_explicit_stub():
    """Not OutreachConnector. 'Not configured' and 'delivered' must never look the same."""
    from nexus.integrations.sep import StubSEPConnector, get_sep_connector, set_sep_connector

    set_sep_connector(None)
    conn = get_sep_connector()
    assert isinstance(conn, StubSEPConnector)
    assert conn.platform == "stub"


def test_sep_override_is_distinguishable_from_the_memoized_default():
    from nexus.integrations.sep import (
        StubSEPConnector, get_sep_connector, get_sep_connector_override, set_sep_connector,
    )

    set_sep_connector(None)
    assert get_sep_connector_override() is None
    memoized = get_sep_connector()
    assert memoized is get_sep_connector()
    assert get_sep_connector_override() is None

    installed = StubSEPConnector()
    set_sep_connector(installed)
    assert get_sep_connector_override() is installed
    set_sep_connector(None)


# ---- real adapters ------------------------------------------------------------------------
async def test_salesloft_without_a_key_fails_rather_than_recording():
    from nexus.integrations.sep import SalesloftConnector

    conn = SalesloftConnector()
    res = await conn.push_contact(sequence="Q3", email="a@b.com", payload={})
    assert res.ok is False
    assert conn.pushed == []          # nothing recorded — it did not "succeed"
    assert (await conn.test_connection()).ok is False


async def test_salesloft_test_connection_maps_statuses():
    from nexus.integrations.sep import SalesloftConnector

    conn = SalesloftConnector(token="key")
    conn._request = _fixed(200, {"data": {"email": "rep@acme.com"}})  # type: ignore[method-assign]
    ok = await conn.test_connection()
    assert ok.ok is True and "rep@acme.com" in ok.label

    conn._request = _fixed(401, {})  # type: ignore[method-assign]
    assert "Invalid or expired" in (await conn.test_connection()).detail


async def test_salesloft_reports_a_missing_cadence_by_name():
    """A cadence name that does not exist is user-fixable; a generic failure would send them to
    check their API key instead."""
    from nexus.integrations.sep import SalesloftConnector

    conn = SalesloftConnector(token="key")

    async def _req(method, path, body=None):
        if path.startswith("/v2/people.json?"):
            return 200, {"data": [{"id": 7}]}
        if path.startswith("/v2/cadences.json"):
            return 200, {"data": []}          # no such cadence
        return 200, {}

    conn._request = _req  # type: ignore[method-assign]
    res = await conn.push_contact(sequence="Nope", email="a@b.com", payload={})
    assert res.ok is False
    assert "no cadence named 'Nope'" in res.detail["error"]


async def test_sep_adapters_never_raise_across_the_boundary():
    from nexus.integrations.sep import OutreachConnector, SalesloftConnector

    for conn in (SalesloftConnector(token="k"), OutreachConnector(token="t")):
        async def boom(method, path, body=None):
            raise RuntimeError("socket exploded")

        conn._request = boom  # type: ignore[method-assign]
        res = await conn.push_contact(sequence="Q3", email="a@b.com", payload={})
        assert res.ok is False
        assert (await conn.test_connection()).ok is False


# ---- per-tenant resolution ----------------------------------------------------------------
async def _store(tid: str, key: str, provider: str = "salesloft"):
    from nexus.integrations.sep_credentials import store_credentials

    async with tenant_session(tid) as ts:
        await store_credentials(ts, provider=provider, api_key=key)


async def test_resolve_falls_back_to_the_default_without_a_credential():
    from nexus.integrations.sep import get_sep_connector
    from nexus.integrations.sep_credentials import resolve_sep_connector

    tid = await make_tenant(slug="sf1", name="SF1")
    async with tenant_session(tid) as ts:
        assert await resolve_sep_connector(ts) is get_sep_connector()


async def test_two_tenants_resolve_to_different_sep_connectors():
    from nexus.integrations.sep_credentials import resolve_sep_connector

    tid_a = await make_tenant(slug="sa", name="SA")
    tid_b = await make_tenant(slug="sb", name="SB")
    await _store(tid_a, "key-AAA")
    await _store(tid_b, "key-BBB")

    async with tenant_session(tid_a) as ts:
        ca = await resolve_sep_connector(ts)
    async with tenant_session(tid_b) as ts:
        cb = await resolve_sep_connector(ts)

    assert ca is not cb
    assert ca._token == "key-AAA"
    assert cb._token == "key-BBB"


async def test_a_crm_row_is_not_visible_to_the_sep_resolver():
    """The store is shared; the kind discriminator is what keeps the two apart."""
    from nexus.ingestion.crm_crypto import seal_crm_secret
    from nexus.integrations.sep import get_sep_connector
    from nexus.integrations.sep_credentials import resolve_sep_connector
    from nexus.models.integration import IntegrationConnection

    tid = await make_tenant(slug="crmonly", name="CO")
    async with tenant_session(tid) as ts:
        ts.add(IntegrationConnection(tenant_id=tid, kind="crm", provider="hubspot",
                                     secret=seal_crm_secret({"access_token": "pat"})))
        await ts.flush()
        assert await resolve_sep_connector(ts) is get_sep_connector()


# ---- endpoints ----------------------------------------------------------------------------
_KEY = "sl-key-super-secret"


async def test_sep_connection_never_returns_the_key(client):
    h = auth(await signup(client))
    r = await client.put("/api/integrations/sep/connection", headers=h,
                         json={"provider": "salesloft", "api_key": _KEY})
    assert r.status_code == 200, r.text
    assert _KEY not in r.text

    g = await client.get("/api/integrations/sep/connection", headers=h)
    assert _KEY not in g.text
    body = g.json()
    assert body["source"] == "tenant"
    assert body["provider"] == "salesloft"
    assert body["has_credentials"] is True


async def test_sep_key_is_ciphertext_in_the_database(client):
    from nexus.models.integration import IntegrationConnection

    token = await signup(client)
    await client.put("/api/integrations/sep/connection", headers=auth(token),
                     json={"provider": "salesloft", "api_key": _KEY})
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        row = await ts.first(IntegrationConnection, IntegrationConnection.kind == "sep")
        assert _KEY not in str(row.secret)


async def test_sep_default_source_when_unconfigured(client):
    h = auth(await signup(client))
    g = (await client.get("/api/integrations/sep/connection", headers=h)).json()
    assert g["source"] == "default"
    assert g["has_credentials"] is False


async def test_outreach_is_refused_with_an_api_key(client):
    """Outreach has no API-key path; a pasted key would 401 with nothing to explain why."""
    h = auth(await signup(client))
    r = await client.put("/api/integrations/sep/connection", headers=h,
                         json={"provider": "outreach", "api_key": "x"})
    assert r.status_code == 400
    assert "OAuth" in r.json()["detail"]


async def test_sep_rejects_unknown_provider_and_requires_a_key(client):
    h = auth(await signup(client))
    bad = await client.put("/api/integrations/sep/connection", headers=h,
                           json={"provider": "apollo", "api_key": "x"})
    assert bad.status_code == 400 and "Unknown SEP provider" in bad.json()["detail"]

    missing = await client.put("/api/integrations/sep/connection", headers=h,
                               json={"provider": "salesloft"})
    assert missing.status_code == 400 and "API key is required" in missing.json()["detail"]


async def test_sep_delete_falls_back_to_the_default(client):
    h = auth(await signup(client))
    await client.put("/api/integrations/sep/connection", headers=h,
                     json={"provider": "salesloft", "api_key": _KEY})
    assert (await client.delete("/api/integrations/sep/connection", headers=h)).status_code == 204
    g = (await client.get("/api/integrations/sep/connection", headers=h)).json()
    assert g["source"] == "default"


async def test_rep_cannot_touch_the_sep_connection(client):
    owner_h = auth(await signup(client, slug="sr", email="owner@sr.com", company="SR"))
    invite = await client.post("/api/workspace/members", headers=owner_h, json={
        "email": "rep@sr.com", "full_name": "Rep", "role": "rep", "password": "password123"})
    assert invite.status_code in (200, 201), invite.text
    login = await client.post("/api/auth/login",
                              json={"email": "rep@sr.com", "password": "password123"})
    rep_h = auth(login.json()["access_token"])

    assert (await client.get("/api/integrations/sep/connection", headers=rep_h)).status_code == 403
    assert (await client.put("/api/integrations/sep/connection", headers=rep_h,
                             json={"provider": "salesloft", "api_key": "x"})).status_code == 403
    assert (await client.post("/api/integrations/sep/connection/test",
                              headers=rep_h)).status_code == 403
    assert (await client.delete("/api/integrations/sep/connection",
                                headers=rep_h)).status_code == 403


async def test_one_tenant_cannot_see_anothers_sep_connection(client):
    h_a = auth(await signup(client, slug="sia", email="a@sia.com", company="SIA"))
    h_b = auth(await signup(client, slug="sib", email="b@sib.com", company="SIB"))
    await client.put("/api/integrations/sep/connection", headers=h_a,
                     json={"provider": "salesloft", "api_key": _KEY})
    g = (await client.get("/api/integrations/sep/connection", headers=h_b)).json()
    assert g["has_credentials"] is False
