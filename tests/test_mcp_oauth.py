# tests/test_mcp_oauth.py
"""NEXUS's OAuth 2.1 authorization server, driven the way Claude and ChatGPT drive it
(spec 2026-10-13, task 8)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.mcp import credentials, oauth
from tests.conftest import auth, signup

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _clean():
    from nexus.core import ratelimit

    credentials.forget()
    ratelimit.reset_rate_limits()
    yield
    credentials.forget()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).decode().rstrip("=")
    return verifier, challenge


async def _register(client, redirects=(REDIRECT,), **extra):
    return await client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": list(redirects), **extra})


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def _authorize(client, client_id, challenge, redirect=REDIRECT, **extra):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-123",
              "scope": "nexus.read nexus.research nexus.write offline_access", **extra}
    return await client.get("/oauth/authorize", params=params, follow_redirects=False)


async def _consent(client, token, location, scopes=("read",)):
    request = _query(location)["request"]
    r = await client.post("/api/connections/oauth/approve", headers=auth(token),
                          json={"request": request, "scopes": list(scopes)})
    assert r.status_code == 200, r.text
    return r.json()["redirect_to"]


async def _full_flow(client, token, scopes=("read",)):
    reg = (await _register(client)).json()
    verifier, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    back = await _consent(client, token, loc, scopes)
    q = _query(back)
    tokens = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": reg["client_id"]})
    return reg, tokens, q


# ---- metadata --------------------------------------------------------------------------------

async def test_the_metadata_documents(client):
    prm = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert prm["resource"] == "http://test/mcp"
    assert prm["authorization_servers"] == ["http://test"]
    assert (await client.get("/.well-known/oauth-protected-resource/mcp")).json() == prm
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert asm["issuer"] == "http://test"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["client_id_metadata_document_supported"] is True
    assert "none" in asm["token_endpoint_auth_methods_supported"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert "offline_access" in asm["scopes_supported"]
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint",
                "revocation_endpoint"):
        assert asm[key].startswith("http://test/oauth/")


async def test_the_public_address_wins_when_set(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_base_url", "https://gtm.example.com")
    prm = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert prm["resource"] == "https://gtm.example.com/mcp"


# ---- registration ----------------------------------------------------------------------------

async def test_dynamic_registration_of_a_public_client(client):
    r = await _register(client, ("http://localhost:3118/callback", REDIRECT))
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"].startswith("dcr_") and "client_secret" not in body


async def test_registration_has_its_own_limit_above_the_sign_in_one(client, monkeypatch):
    """An AI app registers from a few server addresses for all its users, so registration must not
    share the 10-a-minute sign-in limit."""
    import nexus.api.routers.mcp as mcp_router

    for _ in range(12):
        assert (await _register(client)).status_code == 201
    monkeypatch.setattr(mcp_router, "DCR_PER_MINUTE", 12)
    limited = await _register(client)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


@pytest.mark.parametrize("uri", ["http://example.com/cb", "https://a.example.com/cb#frag",
                                 "javascript:alert(1)", "/relative"])
async def test_unsafe_redirects_are_refused(client, uri):
    r = await _register(client, (uri,))
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


async def test_a_confidential_registration_gets_a_secret_once(client):
    body = (await _register(client, token_endpoint_auth_method="client_secret_post")).json()
    assert body["client_secret"].startswith("nxs_")


# ---- authorize -------------------------------------------------------------------------------

async def test_an_unknown_client_is_an_error_page_not_a_redirect(client):
    _, challenge = _pkce()
    r = await _authorize(client, "dcr_nope", challenge)
    assert r.status_code == 400 and "location" not in r.headers
    assert "cannot connect" in r.text


async def test_an_unregistered_redirect_is_an_error_page(client):
    reg = (await _register(client)).json()
    _, challenge = _pkce()
    r = await _authorize(client, reg["client_id"], challenge, redirect="https://evil.example/cb")
    assert r.status_code == 400 and "location" not in r.headers


async def test_missing_pkce_redirects_back_with_an_error_and_the_state(client):
    reg = (await _register(client)).json()
    r = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "state": "keep-me"}, follow_redirects=False)
    assert r.status_code == 302
    q = _query(r.headers["location"])
    assert q["error"] == "invalid_request" and q["state"] == "keep-me" and q["iss"] == "http://test"


async def test_a_foreign_resource_is_refused(client):
    reg = (await _register(client)).json()
    _, challenge = _pkce()
    r = await _authorize(client, reg["client_id"], challenge, resource="https://other.example/mcp")
    assert _query(r.headers["location"])["error"] == "invalid_target"


async def test_a_valid_request_goes_to_the_consent_page(client):
    reg = (await _register(client)).json()
    _, challenge = _pkce()
    r = await _authorize(client, reg["client_id"], challenge, resource="http://test/mcp")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http://test/oauth/consent?request=")


async def test_the_consent_view_names_the_host_and_flags_an_unverified_app(client):
    token = await signup(client, slug="oa1", email="o@oa1.com")
    reg = (await _register(client, ("http://127.0.0.1:5000/cb",))).json()
    _, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge,
                            redirect="http://127.0.0.1:5000/cb")).headers["location"]
    view = (await client.get("/api/connections/oauth/request", headers=auth(token),
                             params={"request": _query(loc)["request"]})).json()
    assert view["client_name"] == "Claude" and view["verified"] is False
    assert view["redirect_host"] == "127.0.0.1" and view["only_loopback"] is True
    assert view["user_email"] == "o@oa1.com" and view["scopes"] == ["read", "research", "write"]


# ---- the whole flow --------------------------------------------------------------------------

async def test_the_full_flow_issues_tokens_that_work_with_the_granted_permissions(client):
    token = await signup(client, slug="oa2", email="o@oa2.com")
    reg, tokens, q = await _full_flow(client, token, scopes=("read",))
    assert q["state"] == "st-123" and q["iss"] == "http://test"
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["token_type"] == "Bearer" and body["scope"] == "nexus.read"
    assert body["access_token"].startswith("nxo_") and body["refresh_token"].startswith("nxr_")
    assert tokens.headers["cache-control"] == "no-store"
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {body['access_token']}"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 11
    listed = (await client.get("/api/connections", headers=auth(token))).json()
    assert listed[0]["kind"] == "oauth" and listed[0]["client_name"] == "Claude"


async def test_refresh_rotates_and_reuse_revokes_the_family(client):
    token = await signup(client, slug="oa3", email="o@oa3.com")
    reg, tokens, _ = await _full_flow(client, token)
    first = tokens.json()
    second = (await client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": reg["client_id"]})).json()
    assert second["refresh_token"] != first["refresh_token"]
    replay = await client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": reg["client_id"]})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    credentials.forget()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {second['access_token']}"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


async def test_a_wrong_verifier_a_reused_code_and_a_moved_redirect_all_fail(client):
    token = await signup(client, slug="oa4", email="o@oa4.com")
    reg = (await _register(client)).json()
    verifier, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    code = _query(await _consent(client, token, loc))["code"]
    base = {"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"]}
    wrong = await client.post("/oauth/token", data={**base, "redirect_uri": REDIRECT,
                                                    "code_verifier": "x" * 50})
    assert wrong.json()["error"] == "invalid_grant"
    moved = await client.post("/oauth/token", data={**base, "redirect_uri": "https://claude.ai/x",
                                                    "code_verifier": verifier})
    assert moved.json()["error"] == "invalid_grant"
    good = await client.post("/oauth/token", data={**base, "redirect_uri": REDIRECT,
                                                   "code_verifier": verifier})
    assert good.status_code == 200
    again = await client.post("/oauth/token", data={**base, "redirect_uri": REDIRECT,
                                                    "code_verifier": verifier})
    assert again.json()["error"] == "invalid_grant"
    # The replay revoked what the code had issued.
    credentials.forget()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {good.json()['access_token']}"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


async def test_an_expired_code_fails(client, monkeypatch):
    token = await signup(client, slug="oa5", email="o@oa5.com")
    reg = (await _register(client)).json()
    verifier, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    monkeypatch.setattr(oauth, "CODE_TTL", timedelta(seconds=-1))
    code = _query(await _consent(client, token, loc))["code"]
    r = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": reg["client_id"]})
    assert r.json()["error"] == "invalid_grant"


async def test_a_confidential_client_must_authenticate(client):
    token = await signup(client, slug="oa6", email="o@oa6.com")
    reg = (await _register(client, token_endpoint_auth_method="client_secret_basic")).json()
    verifier, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    code = _query(await _consent(client, token, loc))["code"]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "code_verifier": verifier, "client_id": reg["client_id"]}
    no_secret = await client.post("/oauth/token", data=form)
    assert no_secret.status_code == 401 and no_secret.json()["error"] == "invalid_client"
    basic = base64.b64encode(f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
    ok = await client.post("/oauth/token", data={k: v for k, v in form.items()
                                                 if k != "client_id"},
                           headers={"Authorization": f"Basic {basic}"})
    assert ok.status_code == 200, ok.text


async def test_deny_sends_the_user_back_with_access_denied(client):
    token = await signup(client, slug="oa7", email="o@oa7.com")
    reg = (await _register(client)).json()
    _, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    r = await client.post("/api/connections/oauth/deny", headers=auth(token),
                          json={"request": _query(loc)["request"]})
    q = _query(r.json()["redirect_to"])
    assert q["error"] == "access_denied" and q["state"] == "st-123"


async def test_an_impersonation_session_cannot_approve(client):
    from nexus.core.security import create_impersonation_token
    from tests.conftest import principal_from_token

    token = await signup(client, slug="oa8", email="o@oa8.com")
    p = principal_from_token(token)
    imp = create_impersonation_token(user_id=p.user_id, tenant_id=p.tenant_id, role=p.role,
                                     impersonator_id="staff-1")
    reg = (await _register(client)).json()
    _, challenge = _pkce()
    loc = (await _authorize(client, reg["client_id"], challenge)).headers["location"]
    r = await client.post("/api/connections/oauth/approve", headers=auth(imp),
                          json={"request": _query(loc)["request"], "scopes": ["read"]})
    assert r.status_code == 403


async def test_revocation_endpoint_stops_a_token(client):
    token = await signup(client, slug="oa9", email="o@oa9.com")
    _, tokens, _ = await _full_flow(client, token)
    access = tokens.json()["access_token"]
    assert (await client.post("/oauth/revoke", data={"token": access})).status_code == 200
    assert (await client.post("/oauth/revoke", data={"token": "nope"})).status_code == 200
    credentials.forget()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {access}"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


async def test_revoking_the_connection_in_nexus_stops_its_tokens(client):
    token = await signup(client, slug="oa10", email="o@oa10.com")
    reg, tokens, _ = await _full_flow(client, token)
    cid = (await client.get("/api/connections", headers=auth(token))).json()[0]["id"]
    assert (await client.delete(f"/api/connections/{cid}", headers=auth(token))).status_code == 204
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {tokens.json()['access_token']}"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    refresh = await client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens.json()["refresh_token"],
        "client_id": reg["client_id"]})
    assert refresh.json()["error"] == "invalid_grant"


# ---- Client ID Metadata Documents ------------------------------------------------------------

CIMD_URL = "https://claude.ai/oauth/claude-code-client-metadata"


def _fake_cimd(monkeypatch, doc: dict, status: int = 200):
    from nexus.mcp import cimd

    async def _get(url):
        return status, {"cache-control": "max-age=7200"}, json.dumps(doc).encode()

    monkeypatch.setattr(cimd, "_get", _get)
    monkeypatch.setattr("nexus.sources.safety._is_blocked_host",
                        lambda host, allow_private: (False, ""))


async def test_a_cimd_client_is_fetched_and_loopback_matches_any_port(client, monkeypatch):
    _fake_cimd(monkeypatch, {"client_id": CIMD_URL, "client_name": "Claude Code",
                             "redirect_uris": ["http://localhost/callback",
                                               "http://127.0.0.1/callback"]})
    token = await signup(client, slug="oa11", email="o@oa11.com")
    verifier, challenge = _pkce()
    r = await _authorize(client, CIMD_URL, challenge, redirect="http://localhost:53682/callback")
    assert r.status_code == 302, r.text
    view = (await client.get("/api/connections/oauth/request", headers=auth(token),
                             params={"request": _query(r.headers["location"])["request"]})).json()
    assert view["client_name"] == "Claude Code" and view["verified"] is True
    back = await _consent(client, token, r.headers["location"], ("read", "write"))
    tokens = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": _query(back)["code"],
        "redirect_uri": "http://localhost:53682/callback", "code_verifier": verifier,
        "client_id": CIMD_URL})
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == "nexus.read nexus.write"


async def test_a_cimd_document_naming_another_client_is_refused(client, monkeypatch):
    _fake_cimd(monkeypatch, {"client_id": "https://evil.example/x.json", "client_name": "Claude",
                             "redirect_uris": [REDIRECT]})
    _, challenge = _pkce()
    r = await _authorize(client, CIMD_URL, challenge)
    assert r.status_code == 400 and "does not match" in r.text


def test_redirect_matching_rules():
    reg = ["http://localhost/callback", "https://claude.ai/api/mcp/auth_callback"]
    assert oauth.redirect_matches(reg, "http://localhost:4000/callback")
    assert oauth.redirect_matches(reg, "https://claude.ai/api/mcp/auth_callback")
    assert not oauth.redirect_matches(reg, "http://localhost:4000/other")
    assert not oauth.redirect_matches(reg, "https://claude.ai:8443/api/mcp/auth_callback")
    assert not oauth.redirect_matches(reg, "http://evil.localhost.example/callback")
    assert oauth.redirect_matches(["http://127.0.0.1/cb"], "http://127.0.0.1:9/cb")
    assert not oauth.redirect_matches(["http://127.0.0.1/cb"], "http://localhost:9/cb")


def test_verification_needs_more_than_a_name():
    from nexus.models.mcp import OAuthClient

    def client(kind, client_id, uris):
        return OAuthClient(kind=kind, client_id=client_id, client_name="Claude", redirect_uris=uris)

    assert oauth.is_verified(client("registered", "nxc_1", ["https://x.example/cb"]))
    assert oauth.is_verified(client("cimd", CIMD_URL, ["http://localhost/callback"]))
    assert oauth.is_verified(client("dcr", "dcr_1", [REDIRECT]))
    assert not oauth.is_verified(client("dcr", "dcr_2", [REDIRECT, "https://evil.example/cb"]))
    assert not oauth.is_verified(client("dcr", "dcr_3", ["http://localhost:5000/cb"]))
    assert not oauth.is_verified(client("cimd", "https://evil.example/claude.json",
                                        ["https://evil.example/cb"]))


def test_pkce():
    verifier, challenge = _pkce()
    assert oauth.pkce_ok(verifier, challenge)
    assert not oauth.pkce_ok(verifier + "x", challenge)
    assert not oauth.pkce_ok("short", challenge)


# ---- the ChatGPT domain check ----------------------------------------------------------------

async def test_the_openai_challenge_token(client, monkeypatch):
    from nexus.core.config import get_settings

    assert (await client.get("/.well-known/openai-apps-challenge")).status_code == 404
    monkeypatch.setattr(get_settings(), "openai_apps_challenge_token", "tok-123_abc")
    r = await client.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200 and r.text == "tok-123_abc"
    assert r.headers["content-type"].startswith("text/plain")


# ---- pre-registered clients ------------------------------------------------------------------

async def test_only_a_superadmin_registers_a_confidential_client(client, monkeypatch):
    token = await signup(client, slug="oa12", email="o@oa12.com")
    r = await client.post("/api/admin/oauth-clients", headers=auth(token),
                          json={"name": "Claude (Anthropic-held)", "redirect_uris": [REDIRECT]})
    assert r.status_code == 404
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "o@oa12.com")
    r = await client.post("/api/admin/oauth-clients", headers=auth(token),
                          json={"name": "Claude (Anthropic-held)", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201, r.text
    assert r.json()["client_secret"].startswith("nxs_")
    listed = (await client.get("/api/admin/oauth-clients", headers=auth(token))).json()
    assert listed[0]["client_secret"] == ""
