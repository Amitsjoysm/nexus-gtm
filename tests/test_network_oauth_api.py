from __future__ import annotations

import httpx
import pytest

from tests.conftest import auth, client, signup


async def test_oauth_start_unconfigured_returns_400(client):
    h = auth(await signup(client, slug="oa1", email="r@oa1.com", company="OA1"))
    r = await client.get("/api/network/oauth/google/start", headers=h)
    assert r.status_code == 400
    assert "configured" in r.json()["detail"].lower()


async def test_oauth_start_configured_returns_consent_url(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("NEXUS_NETWORK_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_settings.cache_clear()
    try:
        h = auth(await signup(client, slug="oa2", email="r@oa2.com", company="OA2"))
        r = await client.get("/api/network/oauth/google/start", headers=h)
        assert r.status_code == 200
        url = r.json()["authorize_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "state=" in url and "code_challenge=" in url
    finally:
        get_settings.cache_clear()


async def test_post_accounts_rejects_oauth_provider(client):
    h = auth(await signup(client, slug="oa3", email="r@oa3.com", company="OA3"))
    r = await client.post("/api/network/accounts",
                          json={"provider": "google", "external_account_id": "x"}, headers=h)
    assert r.status_code == 400  # OAuth providers must use the /oauth flow


async def test_linkedin_import(client):
    h = auth(await signup(client, slug="oa4", email="r@oa4.com", company="OA4"))
    acc = (await client.post("/api/network/accounts",
                             json={"provider": "linkedin", "external_account_id": "me"},
                             headers=h)).json()["id"]
    csv = (
        "Notes:\n\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Okafor,https://lnkd.in/ada,ada@helix.com,Helix Health,CTO,01 Jun 2026\n"
    ).encode()
    r = await client.post(
        f"/api/network/accounts/{acc}/import-linkedin",
        files={"file": ("Connections.csv", csv, "text/csv")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_persons"] == 1
