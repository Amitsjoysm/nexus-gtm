"""Forgot/reset password: happy path, enumeration resistance, single-use + expiry, and the
auth-endpoint rate limiter."""
from __future__ import annotations

import pytest

import nexus.auth.password_reset as pr
from nexus.core.config import get_settings
from nexus.core.ratelimit import reset_rate_limits
from tests.conftest import signup


@pytest.fixture
def reset_link(monkeypatch):
    """Capture the reset link the service would email (offline)."""
    box: dict = {}

    async def _fake_send(to: str, link: str) -> bool:
        box["to"] = to
        box["link"] = link
        return True

    monkeypatch.setattr(pr, "send_reset_email", _fake_send)
    return box


def _token_from(link: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(link).query)["token"][0]


async def test_forgot_then_reset_password(client, reset_link):
    await signup(client, slug="reset-co", email="boss@reset-co.com", company="Reset Co")

    r = await client.post("/api/auth/forgot-password", json={"email": "boss@reset-co.com"})
    assert r.status_code == 202
    token = _token_from(reset_link["link"])

    # Reset to a new password.
    rr = await client.post(
        "/api/auth/reset-password",
        json={"email": "boss@reset-co.com", "token": token, "new_password": "newpassword456"},
    )
    assert rr.status_code == 200, rr.text

    # Old password no longer works; new one does.
    bad = await client.post(
        "/api/auth/login", json={"email": "boss@reset-co.com", "password": "password123"}
    )
    assert bad.status_code == 401
    good = await client.post(
        "/api/auth/login", json={"email": "boss@reset-co.com", "password": "newpassword456"}
    )
    assert good.status_code == 200, good.text


async def test_forgot_password_is_enumeration_resistant(client, reset_link):
    # Unknown email: same 202 acknowledgement, and no email is sent.
    r = await client.post("/api/auth/forgot-password", json={"email": "nobody@nowhere.com"})
    assert r.status_code == 202
    assert "link" not in reset_link


async def test_reset_token_is_single_use(client, reset_link):
    await signup(client, slug="once-co", email="once@once-co.com", company="Once Co")
    await client.post("/api/auth/forgot-password", json={"email": "once@once-co.com"})
    token = _token_from(reset_link["link"])
    body = {"email": "once@once-co.com", "token": token, "new_password": "newpassword456"}

    first = await client.post("/api/auth/reset-password", json=body)
    assert first.status_code == 200
    second = await client.post("/api/auth/reset-password", json=body)  # replay
    assert second.status_code == 400


async def test_reset_rejects_bad_token(client, reset_link):
    await signup(client, slug="badtok-co", email="bt@badtok-co.com", company="Badtok Co")
    await client.post("/api/auth/forgot-password", json={"email": "bt@badtok-co.com"})
    r = await client.post(
        "/api/auth/reset-password",
        json={"email": "bt@badtok-co.com", "token": "x" * 40, "new_password": "newpassword456"},
    )
    assert r.status_code == 400


async def test_reset_rejects_expired_token(client, reset_link, monkeypatch):
    monkeypatch.setattr(get_settings(), "password_reset_ttl_s", -1)  # expires immediately
    await signup(client, slug="exp-co", email="exp@exp-co.com", company="Exp Co")
    await client.post("/api/auth/forgot-password", json={"email": "exp@exp-co.com"})
    token = _token_from(reset_link["link"])
    r = await client.post(
        "/api/auth/reset-password",
        json={"email": "exp@exp-co.com", "token": token, "new_password": "newpassword456"},
    )
    assert r.status_code == 400


async def test_login_rate_limited_when_enabled(client, monkeypatch):
    """With the limiter on, repeated logins from one client are throttled to 429."""
    reset_rate_limits()
    monkeypatch.setattr(get_settings(), "auth_rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_max", 3)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_window_s", 60)
    try:
        codes = []
        for _ in range(5):
            r = await client.post(
                "/api/auth/login", json={"email": "nobody@x.com", "password": "wrong"}
            )
            codes.append(r.status_code)
        assert 429 in codes  # throttled before all 5 went through
        assert codes[:3] == [401, 401, 401]  # first 3 within the window pass through to auth
    finally:
        reset_rate_limits()


async def test_rate_limit_off_by_default_keeps_auth_open(client):
    """Default (limiter disabled): many rapid logins are never 429'd — existing flows unaffected."""
    reset_rate_limits()
    for _ in range(8):
        r = await client.post("/api/auth/login", json={"email": "nobody@x.com", "password": "x"})
        assert r.status_code == 401  # plain auth failure, never 429
