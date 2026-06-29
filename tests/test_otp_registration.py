"""Two-step OTP registration: happy path, wrong/expired/exhausted codes, resend cooldown,
duplicate guard, signup gating, and the OTP crypto primitives."""
from __future__ import annotations

import pytest

import nexus.auth.registration as reg
from nexus.auth.otp import generate_otp, hash_otp, verify_otp
from nexus.core.config import get_settings
from tests.conftest import signup

_BODY = {
    "company_name": "Acme Inc",
    "company_slug": "acme-otp",
    "full_name": "Ada Rep",
    "email": "ada@acme-otp.com",
    "password": "password123",
}


@pytest.fixture
def captured(monkeypatch):
    """Capture the OTP the service would email, instead of sending it (offline)."""
    box: dict = {}

    async def _fake_send(to: str, code: str) -> bool:
        box["to"] = to
        box["code"] = code
        return True

    monkeypatch.setattr(reg, "send_otp_email", _fake_send)
    return box


# ---- OTP crypto primitives -------------------------------------------------------------
def test_otp_generate_is_numeric_and_sized():
    code = generate_otp(6)
    assert len(code) == 6 and code.isdigit()
    # Distinct codes across calls (CSPRNG) — not a strict guarantee but vanishingly unlikely to tie.
    assert len({generate_otp(6) for _ in range(20)}) > 1


def test_otp_hash_is_deterministic_and_verifies():
    h = hash_otp("123456", "secret-key")
    assert h == hash_otp("123456", "secret-key")          # deterministic under same key
    assert h != "123456"                                    # never the plaintext
    assert h != hash_otp("123456", "other-key")            # keyed
    assert verify_otp("123456", h, "secret-key") is True
    assert verify_otp("000000", h, "secret-key") is False


# ---- Flow ------------------------------------------------------------------------------
async def test_otp_registration_happy_path(client, captured):
    r = await client.post("/api/auth/register/start", json=_BODY)
    assert r.status_code == 202, r.text
    assert captured["to"] == "ada@acme-otp.com" and captured["code"].isdigit()

    r = await client.post(
        "/api/auth/register/verify", json={"email": _BODY["email"], "code": captured["code"]}
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "owner" and r.json()["access_token"]

    # The verified account can now log in with the password chosen at step 1.
    login = await client.post(
        "/api/auth/login", json={"email": _BODY["email"], "password": _BODY["password"]}
    )
    assert login.status_code == 200, login.text


async def test_otp_wrong_then_correct(client, captured):
    await client.post("/api/auth/register/start", json=_BODY)
    bad = await client.post(
        "/api/auth/register/verify", json={"email": _BODY["email"], "code": "000000"}
    )
    assert bad.status_code == 400 and "attempt" in bad.json()["detail"].lower()
    good = await client.post(
        "/api/auth/register/verify", json={"email": _BODY["email"], "code": captured["code"]}
    )
    assert good.status_code == 201, good.text


async def test_otp_max_attempts_voids_registration(client, captured, monkeypatch):
    monkeypatch.setattr(get_settings(), "otp_max_attempts", 3)
    await client.post("/api/auth/register/start", json=_BODY)
    for _ in range(3):
        r = await client.post(
            "/api/auth/register/verify", json={"email": _BODY["email"], "code": "999999"}
        )
    assert r.status_code == 429  # exhausted -> voided
    # Even the real code no longer works; the pending registration was destroyed.
    after = await client.post(
        "/api/auth/register/verify", json={"email": _BODY["email"], "code": captured["code"]}
    )
    assert after.status_code == 400


async def test_otp_expired_is_rejected(client, captured, monkeypatch):
    monkeypatch.setattr(get_settings(), "otp_ttl_s", -1)  # expires immediately
    await client.post("/api/auth/register/start", json=_BODY)
    r = await client.post(
        "/api/auth/register/verify", json={"email": _BODY["email"], "code": captured["code"]}
    )
    assert r.status_code == 400 and "expired" in r.json()["detail"].lower()


async def test_otp_resend_cooldown(client, captured):
    await client.post("/api/auth/register/start", json=_BODY)
    again = await client.post("/api/auth/register/resend", json={"email": _BODY["email"]})
    assert again.status_code == 429  # within the cooldown window


async def test_register_start_is_enumeration_resistant(client, captured):
    # An existing verified user (via the legacy single-step signup, flag off by default).
    await signup(client, slug="taken-co", email="ada@acme-otp.com", company="Taken Co")
    # register/start for the already-registered email returns the SAME 202 as success and sends
    # no code — an attacker can't tell the email exists.
    r = await client.post("/api/auth/register/start", json=_BODY)
    assert r.status_code == 202
    assert "code" not in captured  # nothing emailed for the existing address


async def test_register_start_rejects_taken_slug(client, captured):
    # Slugs are public workspace URLs, so a taken slug is reported (no enumeration concern).
    await signup(client, slug="acme-otp", email="someone@else.com", company="Acme OTP")
    r = await client.post("/api/auth/register/start", json=_BODY)  # _BODY uses slug "acme-otp"
    assert r.status_code == 409


async def test_password_is_hashed_at_rest(client, captured):
    """The pending row must store a bcrypt hash, never the plaintext password."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import PendingRegistration
    from sqlalchemy import select

    await client.post("/api/auth/register/start", json=_BODY)
    async with get_sessionmaker()() as s:
        row = (await s.scalars(select(PendingRegistration))).first()
    assert row is not None
    assert row.password_hash != _BODY["password"]
    assert row.password_hash.startswith("$2")  # bcrypt
    assert row.otp_hash and row.otp_hash != captured["code"]  # OTP stored one-way


async def test_signup_gated_when_otp_enabled(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "otp_registration_enabled", True)
    r = await client.post("/api/auth/signup", json=_BODY)
    assert r.status_code == 403 and "verification" in r.json()["detail"].lower()
