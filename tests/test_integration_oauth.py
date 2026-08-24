"""OAuth for the CRM/SEP integrations: state signing, configuration gating, and the callback.

The security property under test is that **the callback trusts nothing but the signed state**. It
runs with no Authorization header (the vendor redirects a browser to it), so the tenant it writes
to comes from the state and nowhere else — which makes state verification the whole boundary.
"""
from __future__ import annotations

import pytest

from nexus.integrations import oauth
from tests.conftest import auth, make_tenant, signup, tenant_session


@pytest.fixture
def configured(monkeypatch):
    """A deployment with a HubSpot app registered."""
    from nexus.integrations.settings import get_integration_settings

    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("NEXUS_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_integration_settings.cache_clear()
    yield
    get_integration_settings.cache_clear()


def test_unconfigured_is_inert():
    """No client id means not configured — never a half-built authorize URL."""
    from nexus.integrations.settings import get_integration_settings

    get_integration_settings.cache_clear()
    assert oauth.provider_configured("hubspot") is False
    assert oauth.provider_configured("salesforce") is False
    assert oauth.provider_configured("nope") is False


def test_state_round_trips_and_rejects_tampering():
    state = oauth.sign_state(tenant_id="t1", user_id="u1", kind="crm",
                             provider="hubspot", verifier="v")
    claims = oauth.verify_state(state)
    assert claims["tid"] == "t1" and claims["prov"] == "hubspot" and claims["kind"] == "crm"

    assert oauth.verify_state(state + "x") is None
    assert oauth.verify_state("") is None
    assert oauth.verify_state("not.a.jwt") is None


def test_a_network_oauth_state_is_not_accepted_here():
    """Different ``typ`` claim: a state minted for the network connectors must not authorize a
    CRM callback, even though both are signed with the same app secret."""
    from nexus.network import oauth as network_oauth

    foreign = network_oauth.sign_state(
        member_id="m", tenant_id="t1", provider="google", verifier="v"
    )
    assert oauth.verify_state(foreign) is None


def test_state_expires(monkeypatch):
    state = oauth.sign_state(tenant_id="t1", user_id="u1", kind="crm",
                             provider="hubspot", ttl_s=-1)
    assert oauth.verify_state(state) is None


def test_pkce_challenge_is_the_s256_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = oauth.make_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_carries_the_registered_redirect(configured):
    state = oauth.sign_state(tenant_id="t1", user_id="u1", kind="crm", provider="hubspot")
    url = oauth.authorize_url(kind="crm", provider="hubspot", state=state, challenge="ch")
    assert url.startswith("https://app.hubspot.com/oauth/authorize?")
    assert "client_id=cid" in url
    assert "code_challenge=ch" in url
    assert "crm.objects.companies.read" in url.replace("+", " ").replace("%20", " ")
    # The redirect must match the vendor console exactly.
    assert oauth.redirect_uri("crm", "hubspot") == (
        "https://app.example.com/api/integrations/crm/oauth/hubspot/callback"
    )


def test_expiry_is_stored_absolute_not_relative():
    """A bundle outlives the token response; a relative lifetime read back later means nothing."""
    from nexus.core.db import utcnow

    bundle = oauth._bundle_from_token_response(
        {"access_token": "at", "refresh_token": "rt", "expires_in": 1800}
    )
    assert bundle["access_token"] == "at"
    assert bundle["expires_at"] > int(utcnow().timestamp()) + 1700


def test_needs_refresh_reads_the_stored_expiry():
    from nexus.core.db import utcnow

    now = int(utcnow().timestamp())
    assert oauth.needs_refresh({"access_token": "a", "expires_at": now - 10}) is True
    assert oauth.needs_refresh({"access_token": "a", "expires_at": now + 3600}) is False
    # A pasted private-app token has no expiry and must never be treated as stale.
    assert oauth.needs_refresh({"access_token": "a"}) is False
    # No access token but a refresh token: renew.
    assert oauth.needs_refresh({"refresh_token": "r"}) is True
    assert oauth.needs_refresh({}) is False


# ---- endpoints ----------------------------------------------------------------------------
async def test_start_reports_an_unconfigured_deployment(client):
    from nexus.integrations.settings import get_integration_settings

    get_integration_settings.cache_clear()
    h = auth(await signup(client))
    r = await client.get("/api/integrations/crm/oauth/hubspot/start", headers=h)
    assert r.status_code == 400
    assert "no hubspot app configured" in r.json()["detail"].lower()


async def test_start_returns_an_authorize_url(client, configured):
    h = auth(await signup(client))
    r = await client.get("/api/integrations/crm/oauth/hubspot/start", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["authorize_url"].startswith("https://app.hubspot.com/oauth/authorize?")


async def test_start_404s_for_an_unknown_provider(client, configured):
    h = auth(await signup(client))
    assert (await client.get("/api/integrations/crm/oauth/pipedrive/start",
                             headers=h)).status_code == 404


async def test_start_is_admin_only(client, configured):
    owner_h = auth(await signup(client, slug="oa", email="owner@oa.com", company="OA"))
    invite = await client.post("/api/workspace/members", headers=owner_h, json={
        "email": "rep@oa.com", "full_name": "Rep", "role": "rep", "password": "password123"})
    assert invite.status_code in (200, 201), invite.text
    login = await client.post("/api/auth/login",
                              json={"email": "rep@oa.com", "password": "password123"})
    rep_h = auth(login.json()["access_token"])
    assert (await client.get("/api/integrations/crm/oauth/hubspot/start",
                             headers=rep_h)).status_code == 403


async def test_callback_refuses_an_unsigned_state(client, configured):
    """The callback has no Authorization header, so an unverified state must write nothing."""
    r = await client.get(
        "/api/integrations/crm/oauth/hubspot/callback?code=abc&state=forged"
    )
    assert r.status_code == 400
    assert "Invalid or expired OAuth state" in r.json()["detail"]


async def test_callback_refuses_a_state_minted_for_a_different_kind(client, configured):
    state = oauth.sign_state(tenant_id="t1", user_id="u1", kind="sep", provider="hubspot")
    r = await client.get(
        f"/api/integrations/crm/oauth/hubspot/callback?code=abc&state={state}"
    )
    assert r.status_code == 400


async def test_callback_stores_the_bundle_for_the_tenant_named_in_the_state(
    client, configured, monkeypatch
):
    from nexus.ingestion.crm_credentials import get_connection, has_credentials
    from nexus.integrations import connections

    tid = await make_tenant(slug="cb", name="CB")

    async def fake_exchange(*, kind, provider, code, verifier=""):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_at": 9999999999}

    monkeypatch.setattr(oauth, "exchange_code", fake_exchange)

    state = oauth.sign_state(tenant_id=tid, user_id="u1", kind="crm", provider="hubspot")
    r = await client.get(
        f"/api/integrations/crm/oauth/hubspot/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    assert "connected=hubspot" in r.headers["location"]

    async with tenant_session(tid) as ts:
        row = await get_connection(ts)
        assert row is not None and row.provider == "hubspot"
        assert has_credentials(row)
        assert connections.secret_bundle(row)["refresh_token"] == "rt-new"


async def test_callback_surfaces_a_denied_consent(client, configured):
    tid = await make_tenant(slug="deny", name="DENY")
    state = oauth.sign_state(tenant_id=tid, user_id="u1", kind="crm", provider="hubspot")
    r = await client.get(
        f"/api/integrations/crm/oauth/hubspot/callback"
        f"?error=access_denied&error_description=User+said+no&state={state}",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "error=" in r.headers["location"]


async def test_an_oauth_bundle_reads_as_configured_even_without_a_pasted_token(client, configured):
    """A workspace that connected via OAuth has no access_token the admin typed. Checking a single
    key would have reported it as unconfigured."""
    from nexus.ingestion.crm_credentials import has_credentials
    from nexus.models.integration import IntegrationConnection
    from nexus.ingestion.crm_crypto import seal_crm_secret

    tid = await make_tenant(slug="oauthonly", name="OO")
    async with tenant_session(tid) as ts:
        ts.add(IntegrationConnection(
            tenant_id=tid, kind="crm", provider="hubspot",
            secret=seal_crm_secret({"refresh_token": "rt-only"}),
        ))
        await ts.flush()
        row = await ts.first(IntegrationConnection)
        assert has_credentials(row) is True
