"""Connecting SDR mailboxes: OAuth state, scopes, token lifetimes, error mapping, ownership, API.

Offline: nothing here calls Google or Microsoft. The adapters themselves are exercised against real
mailboxes by `tests_live/engagement/test_mailboxes_live.py` (D21).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def _app(provider: str = "google"):
    from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp

    return OAuthApp(
        provider=provider, client_id="client-id", client_secret="client-secret",
        tenant="common" if provider == "microsoft" else "",
        redirect_uri=f"https://app.example.com/api/engagement/mailboxes/oauth/{provider}/callback",
        scopes=GOOGLE_SCOPES if provider == "google" else MICROSOFT_SCOPES, missing=(),
    )


# ---- OAuth -----------------------------------------------------------------------------------------

def test_the_state_round_trips_and_refuses_another_provider_or_a_network_state():
    from nexus.engagement.mailboxes import oauth
    from nexus.network.oauth import sign_state as network_state

    token = oauth.sign_state(user_id="u1", tenant_id="t1", provider="google", verifier="v",
                             timezone="Europe/London")
    claims = oauth.verify_state(token, provider="google")
    assert (claims["uid"], claims["tid"], claims["pkce"], claims["tz"]) == (
        "u1", "t1", "v", "Europe/London")
    assert oauth.verify_state(token, provider="microsoft") is None
    assert oauth.verify_state(token + "x", provider="google") is None
    foreign = network_state(member_id="u1", tenant_id="t1", provider="google", verifier="v")
    assert oauth.verify_state(foreign, provider="google") is None


def test_authorize_urls_ask_for_offline_access_and_pkce():
    from nexus.engagement.mailboxes import oauth

    google = urlparse(oauth.authorize_url(_app("google"), state="s", challenge="c"))
    q = parse_qs(google.query)
    assert google.netloc == "accounts.google.com"
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["s"]
    assert "https://www.googleapis.com/auth/gmail.compose" in q["scope"][0].split()

    microsoft = urlparse(oauth.authorize_url(_app("microsoft"), state="s", challenge="c"))
    assert microsoft.path == "/common/oauth2/v2.0/authorize"
    assert "offline_access" in parse_qs(microsoft.query)["scope"][0].split()


def test_a_grant_missing_mail_access_is_detected():
    from nexus.engagement.mailboxes import oauth

    full_google = ("openid https://www.googleapis.com/auth/userinfo.email "
                   "https://www.googleapis.com/auth/gmail.readonly "
                   "https://www.googleapis.com/auth/gmail.compose")
    assert oauth.missing_scopes(_app("google"), full_google) == []
    assert oauth.missing_scopes(_app("google"), "openid email") == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    # Microsoft reports Graph scopes without the resource prefix.
    granted = "Mail.ReadWrite Mail.Send User.Read openid profile email offline_access"
    assert oauth.missing_scopes(_app("microsoft"), granted) == []


def test_a_refresh_keeps_the_old_refresh_token_when_the_provider_omits_one():
    from nexus.engagement.mailboxes import oauth

    first = oauth.bundle_from_response({"access_token": "a1", "refresh_token": "r1",
                                        "expires_in": 3599})
    google_refresh = oauth.bundle_from_response({"access_token": "a2", "expires_in": 3599},
                                                previous=first)
    assert google_refresh["refresh_token"] == "r1"
    microsoft_refresh = oauth.bundle_from_response(
        {"access_token": "a3", "refresh_token": "r2", "expires_in": 3599}, previous=first)
    assert microsoft_refresh["refresh_token"] == "r2"


def test_token_lifetimes():
    from nexus.engagement.mailboxes import tokens

    now = 1_800_000_000
    assert tokens.needs_refresh({}, now=now)
    assert tokens.needs_refresh({"access_token": "a", "expires_at": now + 60}, now=now)
    assert not tokens.needs_refresh({"access_token": "a", "expires_at": now + 3000}, now=now)
    assert tokens.liveness_due({"refreshed_at": now - 25 * 3600}, now=now)
    assert not tokens.liveness_due({"refreshed_at": now - 3600}, now=now)


# ---- provider error mapping ------------------------------------------------------------------------

def test_errors_map_to_what_the_caller_must_do():
    from nexus.engagement.mailboxes.provider import (
        AuthExpired,
        NotFound,
        ProviderError,
        ProviderLimit,
        TransientError,
    )
    from nexus.engagement.mailboxes.transport import classify_error

    assert isinstance(classify_error(401, {}, "", now=NOW), AuthExpired)
    assert isinstance(classify_error(404, {}, "", now=NOW), NotFound)
    assert isinstance(classify_error(503, {}, "", now=NOW), TransientError)

    retry = classify_error(429, {"Retry-After": "120"}, "", now=NOW)
    assert isinstance(retry, ProviderLimit) and retry.retry_at == NOW + timedelta(seconds=120)

    stamped = classify_error(
        403, {}, "User-rate limit exceeded.  Retry after 2026-09-17T11:30:00.000Z", now=NOW)
    assert stamped.retry_at == datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)

    daily = classify_error(403, {}, "Daily user sending quota exceeded.", now=NOW)
    assert isinstance(daily, ProviderLimit) and daily.retry_at == NOW + timedelta(hours=24)

    permission = classify_error(403, {}, "Request had insufficient authentication scopes.", now=NOW)
    assert type(permission) is ProviderError


# ---- Outlook conversation index ----------------------------------------------------------------------

def test_a_follow_up_extends_the_parents_conversation_index():
    from nexus.engagement.mailboxes.graph import child_thread_index, new_thread_index

    parent = new_thread_index(NOW)
    raw_parent = base64.b64decode(parent)
    assert len(raw_parent) == 22 and raw_parent[0] == 0x01
    child = base64.b64decode(child_thread_index(parent, NOW + timedelta(hours=26)))
    assert len(child) == 27 and child[:22] == raw_parent
    grandchild = base64.b64decode(child_thread_index(base64.b64encode(child).decode(),
                                                     NOW + timedelta(days=5)))
    assert len(grandchild) == 32 and grandchild[:27] == child
    assert len(base64.b64decode(child_thread_index("not base64!", NOW))) == 22


def test_a_header_is_set_without_touching_the_body():
    from email import message_from_bytes
    from email.message import EmailMessage

    from nexus.engagement.mailboxes.graph import with_header

    original = EmailMessage()
    original["Subject"] = "Re: pricing"
    original["Thread-Index"] = "old"
    original.set_content("Hi Jane,\n\nFollowing up on pricing.\n\nBest,\nSam\n")
    updated = message_from_bytes(with_header(original.as_bytes(), "Thread-Index", "new"))
    assert updated.get_all("Thread-Index") == ["new"]
    assert "Following up on pricing." in updated.get_payload(decode=True).decode()


# ---- service ---------------------------------------------------------------------------------------

async def _owner(client, slug: str):
    token = await signup(client, slug=slug, email=f"sdr@{slug}co.com", company=slug.title())
    return token, principal_from_token(token)


def _bundle():
    return {"access_token": "at-plaintext", "refresh_token": "rt-plaintext",
            "expires_at": 1_900_000_000, "refreshed_at": 1_800_000_000}


async def test_a_connection_is_sealed_and_a_reconnect_updates_the_same_row(client):
    from nexus.engagement.mailboxes.service import upsert_connection
    from nexus.engagement.mailboxes.tokens import unseal

    _token, me = await _owner(client, "seal")
    async with tenant_session(me.tenant_id) as ts:
        first = await upsert_connection(
            ts, owner_user_id=me.user_id, provider="google", email="SDR@SealCo.com",
            display_name="Sam", bundle=_bundle(), scopes=["gmail.readonly"],
            timezone="Europe/London")
        assert first.email == "sdr@sealco.com" and first.timezone == "Europe/London"
        assert "rt-plaintext" not in str(first.tokens) and "enc" in first.tokens
        assert unseal(first.tokens)["refresh_token"] == "rt-plaintext"
        first.status = "needs_reauth"
        await ts.flush()
        again = await upsert_connection(
            ts, owner_user_id=me.user_id, provider="google", email="sdr@sealco.com",
            display_name="Sam", bundle={**_bundle(), "refresh_token": "rt-new"}, scopes=[],
            timezone="America/New_York")
        assert again.id == first.id and again.status == "connected"
        assert again.timezone == "Europe/London"  # a chosen timezone is not overwritten
        assert unseal(again.tokens)["refresh_token"] == "rt-new"


async def test_a_colleagues_mailbox_cannot_be_taken_over(client):
    from nexus.engagement.mailboxes.service import MailboxOwnedByColleague, upsert_connection

    _token, me = await _owner(client, "own")
    async with tenant_session(me.tenant_id) as ts:
        await upsert_connection(ts, owner_user_id=me.user_id, provider="google",
                                email="shared@ownco.com", display_name="", bundle=_bundle(),
                                scopes=[], timezone="UTC")
    with pytest.raises(MailboxOwnedByColleague):
        async with tenant_session(me.tenant_id) as ts:
            await upsert_connection(ts, owner_user_id="someone-else", provider="google",
                                    email="shared@ownco.com", display_name="", bundle=_bundle(),
                                    scopes=[], timezone="UTC")


async def test_edits_are_validated_and_a_disconnect_deletes_the_tokens(client):
    from nexus.engagement.mailboxes import service

    _token, me = await _owner(client, "edit")
    async with tenant_session(me.tenant_id) as ts:
        row = await service.upsert_connection(
            ts, owner_user_id=me.user_id, provider="microsoft", email="sdr@editco.com",
            display_name="", bundle=_bundle(), scopes=[], timezone="UTC")
        with pytest.raises(ValueError, match="not a timezone"):
            await service.update_mailbox(ts, row, timezone="Mars/Olympus")
        with pytest.raises(ValueError, match="between 0.50 and 0.99"):
            await service.update_mailbox(ts, row, reply_confidence=0.3)
        await service.update_mailbox(ts, row, timezone="Asia/Kolkata", reply_confidence=0.9,
                                     signature="Sam\nSDR")
        assert (row.timezone, row.reply_confidence) == ("Asia/Kolkata", 0.9)
        await service.disconnect(ts, row, actor_user_id=me.user_id)
        assert row.status == "revoked" and row.tokens == {}
        assert service.sending_state(row)[0] is False


# ---- API -------------------------------------------------------------------------------------------

async def test_connecting_an_unconfigured_provider_says_what_is_missing(client):
    token, _me = await _owner(client, "noconf")
    providers = await client.get("/api/engagement/mailboxes/providers", headers=auth(token))
    assert providers.json() == [{"provider": "google", "configured": False},
                                {"provider": "microsoft", "configured": False}]
    r = await client.post("/api/engagement/mailboxes/oauth/google/start",
                          json={"timezone": "UTC"}, headers=auth(token))
    assert r.status_code == 409
    assert any("client id" in m for m in r.json()["detail"]["missing"])


async def test_a_configured_provider_returns_an_authorize_url(client, monkeypatch):
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    monkeypatch.setattr(get_settings(), "engagement_google_client_id",
                        "123456789012-abc123.apps.googleusercontent.com")
    await add_key("google_oauth", "", "google-client-secret-value")
    resolver.invalidate()
    token, _me = await _owner(client, "conf")
    r = await client.post("/api/engagement/mailboxes/oauth/google/start",
                          json={"timezone": "Europe/Paris"}, headers=auth(token))
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()["authorize_url"]).query)
    assert query["redirect_uri"] == [
        "https://app.example.com/api/engagement/mailboxes/oauth/google/callback"]
    assert query["state"] and query["code_challenge"]
    resolver.invalidate()


async def test_a_callback_with_a_bad_state_redirects_with_an_error(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    r = await client.get("/api/engagement/mailboxes/oauth/google/callback",
                         params={"code": "c", "state": "forged"})
    assert r.status_code == 302
    assert r.headers["location"] == "https://app.example.com/mailboxes?error=bad_state"


async def test_reps_see_their_own_mailboxes_and_only_managers_the_team(client):
    from nexus.engagement.mailboxes.service import upsert_connection

    token, me = await _owner(client, "list")
    async with tenant_session(me.tenant_id) as ts:
        await upsert_connection(ts, owner_user_id=me.user_id, provider="google",
                                email="sdr@listco.com", display_name="", bundle=_bundle(),
                                scopes=[], timezone="UTC")
    rows = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert [r["email"] for r in rows] == ["sdr@listco.com"] and rows[0]["mine"] is True
    assert "tokens" not in rows[0] and "at-plaintext" not in str(rows)

    from nexus.core.security import create_access_token

    rep = create_access_token(user_id="rep-user", tenant_id=me.tenant_id, role="rep")
    team = await client.get("/api/engagement/mailboxes", params={"team": True},
                            headers=auth(rep))
    assert team.status_code == 403


async def test_the_refresh_job_is_registered_and_idle_without_mailboxes():
    from nexus.workers.tasks import HANDLERS, handle_refresh_mailbox_tokens

    assert "refresh_mailbox_tokens" in HANDLERS
    assert await handle_refresh_mailbox_tokens({}) == {
        "refreshed": 0, "needs_reauth": 0, "failed": 0}


def test_the_mailboxes_page_is_reachable_by_every_member():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
    app = (src / "App.tsx").read_text(encoding="utf-8")
    nav = (src / "app" / "nav.tsx").read_text(encoding="utf-8")
    page = (src / "pages" / "engagement" / "MailboxesPage.tsx").read_text(encoding="utf-8")
    assert 'path="/mailboxes"' in app and 'capability="module.outreach" name="My mailboxes"' in app
    assert '{ to: "/mailboxes", label: "My mailboxes"' in nav
    assert "api.startMailboxConnect" in page and "browserTimezone()" in page
    assert "window.location.assign(authorize_url)" in page
    for code in ("owned_by_colleague", "missing_scopes", "bad_state", "denied"):
        assert code in page, f"the page does not explain {code}"
