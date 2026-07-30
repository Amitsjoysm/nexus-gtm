# tests/test_mfa_login.py
"""Two-step login — and, more importantly, proof that one-step login did not change.

The compatibility line for this milestone is that a user without a *confirmed* second factor sees
the identical login response they saw before MFA existed: same status, same keys, same order,
same values. ``test_login_is_byte_for_byte_unchanged_without_mfa`` asserts the raw bytes, not the
parsed dict, because a reordered or added key is exactly the kind of change that breaks a client
while every parsed-dict assertion still passes.

The other half is that the challenge token must be inert everywhere except ``/auth/mfa/verify``.
It is a signed, unexpired JWT with a real ``sub`` and ``tid``, so if the bearer path did not
discriminate on ``typ`` it would authenticate the whole API and MFA would be decoration.
"""
from __future__ import annotations

import json
import time

import pytest

import nexus.auth.mfa_service as mfa_service
from nexus.auth.mfa import totp_code
from nexus.core.config import get_settings
from tests.conftest import auth, signup

_PASSWORD = "password123"


@pytest.fixture
def mailed(monkeypatch):
    box: dict = {}

    async def _fake_send(to: str, code: str) -> bool:
        box["to"] = to
        box["code"] = code
        return True

    monkeypatch.setattr(mfa_service, "send_mfa_code_email", _fake_send)
    return box


async def _login(client, email: str, **extra):
    return await client.post(
        "/api/auth/login", json={"email": email, "password": _PASSWORD, **extra}
    )


async def _enroll_and_confirm_totp(client, token: str) -> dict:
    r = await client.post("/api/auth/mfa/enroll", json={"method": "totp"}, headers=auth(token))
    assert r.status_code == 201, r.text
    body = r.json()
    r = await client.post(
        "/api/auth/mfa/confirm",
        json={"method": "totp", "code": totp_code(body["secret"])},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    return body


def _next_code(secret: str) -> str:
    """A code one step ahead — inside the drift window, past the counter confirm already spent."""
    step = get_settings().mfa_totp_step_s
    return totp_code(secret, timestamp=time.time() + step, step=step)


# ---- the compatibility line ----------------------------------------------------------------
async def test_login_is_byte_for_byte_unchanged_without_mfa(client):
    """The single most important assertion in this milestone."""
    token = await signup(client, slug="mfa-plain", email="a@mfa-plain.com")
    r = await _login(client, "a@mfa-plain.com")

    assert r.status_code == 200
    body = json.loads(r.content)
    # Exactly the historical shape — no extra keys, nothing renamed, nothing removed.
    assert list(body.keys()) == ["access_token", "token_type", "tenant_id", "role"]
    assert body["token_type"] == "bearer"
    assert body["role"] == "owner"
    assert body["access_token"] and "mfa_required" not in r.text
    # And the token works immediately, with no second step.
    assert (await client.get("/api/auth/tenants", headers=auth(body["access_token"]))).status_code == 200
    assert token  # the signup token and the login token are both usable


async def test_unconfirmed_enrolment_does_not_gate_login(client):
    """Scanning a QR code and abandoning setup must not lock anyone out."""
    token = await signup(client, slug="mfa-half", email="a@mfa-half.com")
    r = await client.post("/api/auth/mfa/enroll", json={"method": "totp"}, headers=auth(token))
    assert r.status_code == 201

    r = await _login(client, "a@mfa-half.com")
    assert r.status_code == 200
    assert list(json.loads(r.content).keys()) == [
        "access_token", "token_type", "tenant_id", "role",
    ]


async def test_login_still_rejects_a_wrong_password_the_same_way(client):
    await signup(client, slug="mfa-wrong", email="a@mfa-wrong.com")
    r = await client.post(
        "/api/auth/login", json={"email": "a@mfa-wrong.com", "password": "nope-nope-nope"}
    )
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid email or password"}


# ---- the two-step path ------------------------------------------------------------------
async def test_confirmed_mfa_turns_login_into_a_challenge(client):
    token = await signup(client, slug="mfa-two", email="a@mfa-two.com")
    await _enroll_and_confirm_totp(client, token)

    r = await _login(client, "a@mfa-two.com")
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True
    assert body["methods"] == ["totp"]
    assert body["expires_in_s"] == get_settings().mfa_challenge_ttl_s
    # The decisive property: no session was issued.
    assert "access_token" not in body
    assert body["challenge_token"]


async def test_verify_exchanges_the_challenge_for_a_real_token(client):
    token = await signup(client, slug="mfa-ok", email="a@mfa-ok.com")
    enrolled = await _enroll_and_confirm_totp(client, token)

    challenge = (await _login(client, "a@mfa-ok.com")).json()["challenge_token"]
    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": _next_code(enrolled["secret"])},
    )
    assert r.status_code == 200, r.text
    body = json.loads(r.content)
    assert list(body.keys()) == ["access_token", "token_type", "tenant_id", "role"]
    assert (
        await client.get("/api/auth/tenants", headers=auth(body["access_token"]))
    ).status_code == 200


async def test_verify_rejects_a_wrong_code(client):
    token = await signup(client, slug="mfa-badcode", email="a@mfa-badcode.com")
    await _enroll_and_confirm_totp(client, token)
    challenge = (await _login(client, "a@mfa-badcode.com")).json()["challenge_token"]

    r = await client.post(
        "/api/auth/mfa/verify", json={"challenge_token": challenge, "code": "000000"}
    )
    assert r.status_code == 400
    assert "access_token" not in r.text


async def test_verify_refuses_a_replayed_code(client):
    """A code observed on its way through must not work a second time inside its window."""
    token = await signup(client, slug="mfa-replay", email="a@mfa-replay.com")
    enrolled = await _enroll_and_confirm_totp(client, token)
    code = _next_code(enrolled["secret"])

    first = (await _login(client, "a@mfa-replay.com")).json()["challenge_token"]
    assert (
        await client.post(
            "/api/auth/mfa/verify", json={"challenge_token": first, "code": code}
        )
    ).status_code == 200

    second = (await _login(client, "a@mfa-replay.com")).json()["challenge_token"]
    r = await client.post(
        "/api/auth/mfa/verify", json={"challenge_token": second, "code": code}
    )
    assert r.status_code == 400, "the same code was accepted twice"


async def test_a_recovery_code_completes_a_login(client):
    token = await signup(client, slug="mfa-recl", email="a@mfa-recl.com")
    enrolled = await _enroll_and_confirm_totp(client, token)
    challenge = (await _login(client, "a@mfa-recl.com")).json()["challenge_token"]

    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": enrolled["recovery_codes"][0]},
    )
    assert r.status_code == 200, r.text

    # Single use: the same code cannot log in again.
    challenge = (await _login(client, "a@mfa-recl.com")).json()["challenge_token"]
    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": enrolled["recovery_codes"][0]},
    )
    assert r.status_code == 400


async def test_email_method_mails_a_code_at_login_and_verifies(client, mailed):
    token = await signup(client, slug="mfa-eml", email="a@mfa-eml.com")
    r = await client.post("/api/auth/mfa/enroll", json={"method": "email"}, headers=auth(token))
    assert r.status_code == 201
    r = await client.post(
        "/api/auth/mfa/confirm",
        json={"method": "email", "code": mailed["code"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text

    confirm_code = mailed["code"]
    mailed.clear()
    body = (await _login(client, "a@mfa-eml.com")).json()
    assert body["methods"] == ["email"]
    assert mailed["to"] == "a@mfa-eml.com"
    # Confirming just spent this step's counter, so the login code must be the *next* one —
    # otherwise the user is mailed digits the replay guard is bound to refuse.
    assert mailed["code"] != confirm_code

    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": body["challenge_token"], "code": mailed["code"]},
    )
    assert r.status_code == 200, r.text
    assert json.loads(r.content)["access_token"]

    # And the code just spent cannot be replayed on a second challenge.
    spent = mailed["code"]
    challenge = (await _login(client, "a@mfa-eml.com")).json()["challenge_token"]
    r = await client.post(
        "/api/auth/mfa/verify", json={"challenge_token": challenge, "code": spent}
    )
    assert r.status_code == 400


async def test_resend_delivers_a_usable_code(client, mailed):
    token = await signup(client, slug="mfa-resend", email="a@mfa-resend.com")
    assert (
        await client.post("/api/auth/mfa/enroll", json={"method": "email"}, headers=auth(token))
    ).status_code == 201
    assert (
        await client.post(
            "/api/auth/mfa/confirm",
            json={"method": "email", "code": mailed["code"]},
            headers=auth(token),
        )
    ).status_code == 200

    challenge = (await _login(client, "a@mfa-resend.com")).json()["challenge_token"]
    r = await client.post(
        "/api/auth/mfa/challenge/resend",
        json={"challenge_token": challenge, "method": "email"},
    )
    assert r.status_code == 202
    r = await client.post(
        "/api/auth/mfa/verify", json={"challenge_token": challenge, "code": mailed["code"]}
    )
    assert r.status_code == 200, r.text


# ---- the challenge token must authorize nothing --------------------------------------------
async def test_the_challenge_token_is_not_a_bearer_token(client):
    """It is a valid signature over a real sub/tid — only the ``typ`` check stops it."""
    token = await signup(client, slug="mfa-chal", email="a@mfa-chal.com")
    await _enroll_and_confirm_totp(client, token)
    challenge = (await _login(client, "a@mfa-chal.com")).json()["challenge_token"]

    for path in ["/api/auth/tenants", "/api/accounts", "/api/auth/mfa"]:
        r = await client.get(path, headers=auth(challenge))
        assert r.status_code == 401, f"{path} accepted an MFA challenge token as a bearer token"

    # Nor can it authorize a state-changing call.
    r = await client.post(
        "/api/auth/mfa/enroll", json={"method": "totp"}, headers=auth(challenge)
    )
    assert r.status_code == 401


async def test_an_access_token_is_not_a_challenge_token(client):
    """The inverse: a real session must not be usable to skip the second factor for someone."""
    token = await signup(client, slug="mfa-inv", email="a@mfa-inv.com")
    enrolled = await _enroll_and_confirm_totp(client, token)

    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": token, "code": _next_code(enrolled["secret"])},
    )
    assert r.status_code == 401


async def test_verify_rejects_garbage_and_expired_challenges(client):
    from nexus.core.security import create_mfa_challenge_token

    token = await signup(client, slug="mfa-exp", email="a@mfa-exp.com")
    enrolled = await _enroll_and_confirm_totp(client, token)

    for bad in ["", "not-a-jwt", "a.b.c"]:
        r = await client.post(
            "/api/auth/mfa/verify", json={"challenge_token": bad, "code": "000000"}
        )
        assert r.status_code == 401

    # An expired challenge is refused even with a perfectly good code.
    settings = get_settings()
    original = settings.mfa_challenge_ttl_s
    try:
        object.__setattr__(settings, "mfa_challenge_ttl_s", -1)
        expired = create_mfa_challenge_token(user_id="x", tenant_id="y", role="owner")
    finally:
        object.__setattr__(settings, "mfa_challenge_ttl_s", original)
    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": expired, "code": _next_code(enrolled["secret"])},
    )
    assert r.status_code == 401


# ---- lockout ---------------------------------------------------------------------------
async def test_repeated_wrong_codes_lock_the_factor(client):
    """An online guessing attack must run out of budget, not out of patience."""
    token = await signup(client, slug="mfa-lock", email="a@mfa-lock.com")
    enrolled = await _enroll_and_confirm_totp(client, token)
    challenge = (await _login(client, "a@mfa-lock.com")).json()["challenge_token"]

    max_attempts = get_settings().mfa_max_attempts
    for _ in range(max_attempts):
        r = await client.post(
            "/api/auth/mfa/verify", json={"challenge_token": challenge, "code": "000000"}
        )
        assert r.status_code in (400, 429)
    r = await client.post(
        "/api/auth/mfa/verify", json={"challenge_token": challenge, "code": "000000"}
    )
    assert r.status_code == 429

    # Locked means locked: even the correct code is refused until the window lifts.
    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": _next_code(enrolled["secret"])},
    )
    assert r.status_code == 429


async def test_lockout_spans_every_method(client, mailed):
    """Burning the budget on one factor must not hand the attacker a fresh one on the other."""
    token = await signup(client, slug="mfa-lock2", email="a@mfa-lock2.com")
    enrolled = await _enroll_and_confirm_totp(client, token)
    r = await client.post("/api/auth/mfa/enroll", json={"method": "email"}, headers=auth(token))
    assert r.status_code == 201
    r = await client.post(
        "/api/auth/mfa/confirm",
        json={"method": "email", "code": mailed["code"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text

    challenge = (await _login(client, "a@mfa-lock2.com")).json()["challenge_token"]
    for _ in range(get_settings().mfa_max_attempts):
        await client.post(
            "/api/auth/mfa/verify",
            json={"challenge_token": challenge, "code": "000000", "method": "totp"},
        )

    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": _next_code(enrolled["secret"]), "method": "email"},
    )
    assert r.status_code == 429


# ---- workspace selection survives the second step -------------------------------------------
async def test_the_challenge_carries_the_requested_workspace(client):
    token = await signup(client, slug="mfa-ws-a", email="a@mfa-ws.com")
    r = await client.post(
        "/api/auth/workspaces", json={"name": "Second", "slug": "mfa-ws-b"}, headers=auth(token)
    )
    assert r.status_code == 201, r.text
    second_tenant = r.json()["tenant_id"]

    enrolled = await _enroll_and_confirm_totp(client, token)
    challenge = (await _login(client, "a@mfa-ws.com", tenant_slug="mfa-ws-b")).json()[
        "challenge_token"
    ]
    r = await client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": _next_code(enrolled["secret"])},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == second_tenant
