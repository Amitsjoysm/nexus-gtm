"""CRM and network OAuth are bound to the browser that pressed Connect (2026-11-20).

The same hole the mailbox connect had (`tests/test_mailbox_security.py`): the signed ``state`` was
the whole credential. Anyone could start "connect HubSpot" or "connect Google", send the provider's
consent link to someone else, and when that person clicked Allow their CRM (every company and
contact, write access) or their contacts and calendar joined the first person's workspace.

Start now also sets a short-lived HttpOnly cookie on the flow's own path holding a nonce and the
PKCE verifier; the state carries only the nonce's hash. The callback exchanges nothing unless the
browser it lands in holds the matching cookie, and the cookie is spent on every outcome.

Offline: the vendors are replaced at the token exchange, the one call the callback makes.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


def _state_of(authorize_url: str) -> str:
    return parse_qs(urlparse(authorize_url).query)["state"][0]


# ---- CRM (HubSpot / Salesforce) -------------------------------------------------------------------

@pytest.fixture
def hubspot_app(monkeypatch):
    from nexus.integrations.settings import get_integration_settings

    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("NEXUS_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_integration_settings.cache_clear()
    yield
    get_integration_settings.cache_clear()


@pytest.fixture
def crm_exchange(monkeypatch):
    from nexus.integrations import oauth

    calls: list[tuple[str, str]] = []

    async def _exchange(*, kind, provider, code, verifier=""):
        calls.append((code, verifier))
        return {"access_token": "victim-at", "refresh_token": "victim-rt",
                "expires_at": 9_999_999_999}

    monkeypatch.setattr(oauth, "exchange_code", _exchange)
    return calls


async def _crm_start(client, token) -> str:
    r = await client.get("/api/integrations/crm/oauth/hubspot/start", headers=auth(token))
    assert r.status_code == 200, r.text
    return _state_of(r.json()["authorize_url"])


async def test_a_crm_consent_link_opened_in_another_browser_connects_nothing(
        client, hubspot_app, crm_exchange):
    from nexus.ingestion.crm_credentials import get_connection

    attacker = await signup(client, slug="crmattk", email="me@crmattk.io", company="Attk")
    state = await _crm_start(client, attacker)
    client.cookies.clear()  # the victim's browser
    r = await client.get("/api/integrations/crm/oauth/hubspot/callback",
                         params={"code": "victim-code", "state": state}, follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    location = unquote(r.headers["location"])
    assert "connected=" not in location and "different browser" in location
    assert crm_exchange == [], "no code is exchanged, so no token for the victim exists"
    async with tenant_session(principal_from_token(attacker).tenant_id) as ts:
        assert await get_connection(ts) is None


async def test_the_browser_that_pressed_connect_connects_its_crm_once(
        client, hubspot_app, crm_exchange):
    from nexus.ingestion.crm_credentials import get_connection, has_credentials

    token = await signup(client, slug="crmown", email="me@crmown.io", company="Own")
    state = await _crm_start(client, token)
    r = await client.get("/api/integrations/crm/oauth/hubspot/callback",
                         params={"code": "my-code", "state": state}, follow_redirects=False)
    assert "connected=hubspot" in r.headers["location"], r.headers["location"]
    assert len(crm_exchange) == 1 and crm_exchange[0][1], "PKCE runs with the browser's verifier"
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        row = await get_connection(ts)
        assert row is not None and has_credentials(row)
    again = await client.get("/api/integrations/crm/oauth/hubspot/callback",
                             params={"code": "my-code", "state": state}, follow_redirects=False)
    assert "connected=" not in again.headers["location"]
    assert len(crm_exchange) == 1, "the binding is spent"


async def test_the_crm_consent_link_carries_no_pkce_verifier(client, hubspot_app):
    from jose import jwt

    token = await signup(client, slug="crmpkce", email="me@crmpkce.io", company="Pkce")
    claims = jwt.get_unverified_claims(await _crm_start(client, token))
    assert not claims.get("pkce") and claims.get("nh")
    cookie = next(c for c in client.cookies.jar if c.name == "nexus_oauth_crm_hubspot")
    assert cookie.path == "/api/integrations/crm/oauth/hubspot"
    assert cookie.has_nonstandard_attr("HttpOnly")


async def test_a_cookie_from_another_flow_does_not_unlock_this_one(
        client, hubspot_app, crm_exchange):
    """Two workspaces' Connect in one browser: the second start overwrites the first cookie, so
    the first workspace's link no longer matches. Never the wrong workspace."""
    first = await signup(client, slug="crmone", email="a@crmone.io", company="One")
    first_state = await _crm_start(client, first)
    second = await signup(client, slug="crmtwo", email="b@crmtwo.io", company="Two")
    await _crm_start(client, second)
    r = await client.get("/api/integrations/crm/oauth/hubspot/callback",
                         params={"code": "c", "state": first_state}, follow_redirects=False)
    assert "connected=" not in r.headers["location"]
    assert crm_exchange == []


# ---- Network (Google / Microsoft contacts and calendar) ---------------------------------------

@pytest.fixture
def google_app(monkeypatch):
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("NEXUS_NETWORK_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def network_exchange(monkeypatch):
    from nexus.network.connectors.oauthbase import OAuthConnector

    calls: list[tuple[str, str]] = []

    async def _exchange(self, *, code, code_verifier):
        calls.append((code, code_verifier))
        return {"access_token": "victim-at", "refresh_token": "victim-rt", "expires_in": 3600}

    monkeypatch.setattr(OAuthConnector, "exchange_code", _exchange)
    return calls


async def _network_start(client, token) -> str:
    r = await client.get("/api/network/oauth/google/start", headers=auth(token))
    assert r.status_code == 200, r.text
    return _state_of(r.json()["authorize_url"])


async def test_a_network_consent_link_opened_in_another_browser_connects_nothing(
        client, google_app, network_exchange):
    from nexus.models.network import NetworkSourceAccount

    attacker = await signup(client, slug="netattk", email="me@netattk.io", company="Attk")
    state = await _network_start(client, attacker)
    client.cookies.clear()
    r = await client.get("/api/network/oauth/google/callback",
                         params={"code": "victim-code", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "https://app.example.com/network?error=wrong_browser"
    assert network_exchange == []
    async with tenant_session(principal_from_token(attacker).tenant_id) as ts:
        assert await ts.list(NetworkSourceAccount) == []


async def test_the_browser_that_pressed_connect_connects_its_network_once(
        client, google_app, network_exchange):
    from nexus.models.network import NetworkSourceAccount

    token = await signup(client, slug="netown", email="me@netown.io", company="Own")
    state = await _network_start(client, token)
    r = await client.get("/api/network/oauth/google/callback",
                         params={"code": "my-code", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "https://app.example.com/network?connected=google"
    assert len(network_exchange) == 1 and network_exchange[0][1]
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        rows = await ts.list(NetworkSourceAccount)
    assert [(a.provider, a.status) for a in rows] == [("google", "connected")]
    again = await client.get("/api/network/oauth/google/callback",
                             params={"code": "my-code", "state": state}, follow_redirects=False)
    assert again.headers["location"].endswith("error=wrong_browser")
    assert len(network_exchange) == 1


async def test_the_network_consent_link_carries_no_pkce_verifier(client, google_app):
    from jose import jwt

    token = await signup(client, slug="netpkce", email="me@netpkce.io", company="Pkce")
    claims = jwt.get_unverified_claims(await _network_start(client, token))
    assert not claims.get("pkce") and claims.get("nh")
    cookie = next(c for c in client.cookies.jar if c.name == "nexus_oauth_net_google")
    assert cookie.path == "/api/network/oauth/google"
    assert cookie.has_nonstandard_attr("HttpOnly")


# ---- the screens --------------------------------------------------------------------------------

def test_both_screens_say_why_nothing_was_connected():
    from pathlib import Path

    pages = Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages"
    network = (pages / "NetworkPage.tsx").read_text(encoding="utf-8")
    assert "wrong_browser" in network
    integrations = (pages / "IntegrationsPage.tsx").read_text(encoding="utf-8")
    assert 'get("connected")' in integrations and 'get("error")' in integrations
