# tests/test_mfa_enrollment.py
"""Enrolment, confirmation, disable and recovery-code regeneration.

The invariants under test are the ones that decide whether MFA is real or theatre: an
unconfirmed enrolment must not gate anything, a confirmed factor must not be replaceable or
removable without a live code, and neither the seed nor a recovery code may exist in the
database in a form that can be read back.
"""
from __future__ import annotations

import time

import pytest

import nexus.auth.mfa_service as mfa_service
from nexus.auth.mfa import totp_code
from nexus.core.config import get_settings
from tests.conftest import auth, signup

# Enrolment and login exercise many auth calls in quick succession, which is exactly
# what the rate limiter is for. Opt out explicitly rather than weakening the production
# default, which is ON.
pytestmark = pytest.mark.usefixtures("no_auth_rate_limit")


@pytest.fixture
def mailed(monkeypatch):
    """Capture the code the service would email instead of sending it (offline)."""
    box: dict = {}

    async def _fake_send(to: str, code: str) -> bool:
        box["to"] = to
        box["code"] = code
        return True

    monkeypatch.setattr(mfa_service, "send_mfa_code_email", _fake_send)
    return box


async def _enroll_totp(client, token: str) -> dict:
    r = await client.post("/api/auth/mfa/enroll", json={"method": "totp"}, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _next_code(secret: str) -> str:
    """A code for the step *after* now.

    Confirming an enrolment spends the current counter, so the replay guard correctly refuses the
    same code again. One step ahead is still inside the +1 drift window, so the server accepts it.
    """
    step = get_settings().mfa_totp_step_s
    return totp_code(secret, timestamp=time.time() + step, step=step)


async def _confirm(client, token: str, secret: str, method: str = "totp") -> dict:
    code = totp_code(secret, step=get_settings().mfa_totp_step_s)
    r = await client.post(
        "/api/auth/mfa/confirm", json={"method": method, "code": code}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---- status ------------------------------------------------------------------------------
async def test_status_is_empty_for_a_fresh_account(client):
    token = await signup(client, slug="mfa-status", email="a@mfa-status.com")
    r = await client.get("/api/auth/mfa", headers=auth(token))
    assert r.status_code == 200
    assert r.json() == {
        "enabled": False,
        "methods": [],
        "pending_methods": [],
        "recovery_codes_remaining": 0,
    }


# ---- TOTP enrolment ----------------------------------------------------------------------
async def test_totp_enrolment_returns_secret_uri_and_recovery_codes(client):
    token = await signup(client, slug="mfa-totp", email="a@mfa-totp.com")
    body = await _enroll_totp(client, token)

    assert body["method"] == "totp"
    assert body["secret"] and len(body["secret"]) >= 16
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert f"secret={body['secret']}" in body["provisioning_uri"]
    assert len(body["recovery_codes"]) == get_settings().mfa_recovery_code_count
    assert len(set(body["recovery_codes"])) == len(body["recovery_codes"])


async def test_enrolment_is_inert_until_confirmed(client):
    """A user who scans the QR code and closes the tab must not be locked out."""
    token = await signup(client, slug="mfa-pending", email="a@mfa-pending.com")
    await _enroll_totp(client, token)

    r = await client.get("/api/auth/mfa", headers=auth(token))
    assert r.json()["enabled"] is False
    assert r.json()["methods"] == []
    assert r.json()["pending_methods"] == ["totp"]


async def test_confirm_arms_the_factor(client):
    token = await signup(client, slug="mfa-confirm", email="a@mfa-confirm.com")
    body = await _enroll_totp(client, token)
    status_body = await _confirm(client, token, body["secret"])
    assert status_body["enabled"] is True
    assert status_body["methods"] == ["totp"]


async def test_confirm_rejects_a_wrong_code_and_leaves_the_factor_pending(client):
    token = await signup(client, slug="mfa-bad", email="a@mfa-bad.com")
    await _enroll_totp(client, token)
    r = await client.post(
        "/api/auth/mfa/confirm", json={"method": "totp", "code": "000000"}, headers=auth(token)
    )
    assert r.status_code == 400
    assert (await client.get("/api/auth/mfa", headers=auth(token))).json()["enabled"] is False


async def test_confirm_without_an_enrolment_is_404(client):
    token = await signup(client, slug="mfa-none", email="a@mfa-none.com")
    r = await client.post(
        "/api/auth/mfa/confirm", json={"method": "totp", "code": "000000"}, headers=auth(token)
    )
    assert r.status_code == 404


async def test_reenrolling_a_confirmed_method_is_refused(client):
    """Session theft must not be enough to swap the second factor for the attacker's own."""
    token = await signup(client, slug="mfa-re", email="a@mfa-re.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.post("/api/auth/mfa/enroll", json={"method": "totp"}, headers=auth(token))
    assert r.status_code == 409


async def test_restarting_an_unconfirmed_enrolment_rotates_the_seed(client):
    """An abandoned QR scan must stop working once enrolment is restarted."""
    token = await signup(client, slug="mfa-restart", email="a@mfa-restart.com")
    first = await _enroll_totp(client, token)
    second = await _enroll_totp(client, token)
    assert second["secret"] != first["secret"]

    stale = totp_code(first["secret"])
    r = await client.post(
        "/api/auth/mfa/confirm", json={"method": "totp", "code": stale}, headers=auth(token)
    )
    assert r.status_code == 400
    await _confirm(client, token, second["secret"])


async def test_unknown_method_is_rejected_by_schema(client):
    token = await signup(client, slug="mfa-unknown", email="a@mfa-unknown.com")
    r = await client.post("/api/auth/mfa/enroll", json={"method": "sms"}, headers=auth(token))
    assert r.status_code == 422


async def test_enrolment_requires_authentication(client):
    assert (await client.post("/api/auth/mfa/enroll", json={"method": "totp"})).status_code == 403


# ---- email enrolment ---------------------------------------------------------------------
async def test_email_enrolment_mails_a_code_and_confirms(client, mailed):
    token = await signup(client, slug="mfa-email", email="a@mfa-email.com")
    r = await client.post("/api/auth/mfa/enroll", json={"method": "email"}, headers=auth(token))
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["method"] == "email"
    assert body["code_sent"] is True
    assert body["secret"] is None and body["provisioning_uri"] is None  # nothing to scan
    assert body["expires_in_s"] == get_settings().mfa_email_code_step_s
    assert mailed["to"] == "a@mfa-email.com"
    assert mailed["code"].isdigit() and len(mailed["code"]) == get_settings().mfa_totp_digits

    r = await client.post(
        "/api/auth/mfa/confirm",
        json={"method": "email", "code": mailed["code"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["methods"] == ["email"]


async def test_second_enrolment_keeps_the_recovery_codes_already_issued(client, mailed):
    """Adding email to an existing TOTP setup must not silently void the printout on the desk."""
    token = await signup(client, slug="mfa-both", email="a@mfa-both.com")
    first = await _enroll_totp(client, token)
    await _confirm(client, token, first["secret"])

    r = await client.post("/api/auth/mfa/enroll", json={"method": "email"}, headers=auth(token))
    assert r.status_code == 201
    assert r.json()["recovery_codes"] == []

    r = await client.post(
        "/api/auth/mfa/confirm",
        json={"method": "email", "code": mailed["code"]},
        headers=auth(token),
    )
    assert r.status_code == 200
    assert r.json()["methods"] == ["totp", "email"]


# ---- disable -----------------------------------------------------------------------------
async def test_disable_requires_a_live_code(client):
    token = await signup(client, slug="mfa-off", email="a@mfa-off.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.request(
        "DELETE", "/api/auth/mfa", json={"code": "000000"}, headers=auth(token)
    )
    assert r.status_code == 400
    assert (await client.get("/api/auth/mfa", headers=auth(token))).json()["enabled"] is True


async def test_disable_with_a_valid_code_clears_everything(client):
    token = await signup(client, slug="mfa-off2", email="a@mfa-off2.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.request(
        "DELETE", "/api/auth/mfa", json={"code": _next_code(body["secret"])}, headers=auth(token)
    )
    assert r.status_code == 200, r.text

    after = (await client.get("/api/auth/mfa", headers=auth(token))).json()
    assert after["enabled"] is False and after["recovery_codes_remaining"] == 0


async def test_disable_accepts_a_recovery_code(client):
    """The break-glass path: a lost phone must not mean a lost account."""
    token = await signup(client, slug="mfa-rec", email="a@mfa-rec.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.request(
        "DELETE", "/api/auth/mfa", json={"code": body["recovery_codes"][0]}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/auth/mfa", headers=auth(token))).json()["enabled"] is False


async def test_disable_without_mfa_is_404(client):
    token = await signup(client, slug="mfa-off3", email="a@mfa-off3.com")
    r = await client.request(
        "DELETE", "/api/auth/mfa", json={"code": "000000"}, headers=auth(token)
    )
    assert r.status_code == 404


# ---- recovery codes ----------------------------------------------------------------------
async def test_regenerate_replaces_every_previous_code(client):
    token = await signup(client, slug="mfa-regen", email="a@mfa-regen.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.post(
        "/api/auth/mfa/recovery-codes/regenerate",
        json={"code": _next_code(body["secret"])},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    fresh = r.json()["recovery_codes"]
    assert len(fresh) == get_settings().mfa_recovery_code_count
    assert not (set(fresh) & set(body["recovery_codes"]))

    # An old code is now dead: it can no longer disable MFA.
    r = await client.request(
        "DELETE", "/api/auth/mfa", json={"code": body["recovery_codes"][0]}, headers=auth(token)
    )
    assert r.status_code == 400


async def test_regenerate_refuses_a_recovery_code_as_proof(client):
    """A leaked printout must not be able to renew itself into a fresh printout."""
    token = await signup(client, slug="mfa-regen2", email="a@mfa-regen2.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    r = await client.post(
        "/api/auth/mfa/recovery-codes/regenerate",
        json={"code": body["recovery_codes"][0]},
        headers=auth(token),
    )
    assert r.status_code == 400


async def test_a_recovery_code_is_single_use(client):
    token = await signup(client, slug="mfa-once", email="a@mfa-once.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])
    spent = body["recovery_codes"][0]

    # Spend it on a regenerate-style check by disabling, then re-enrol and confirm the same
    # plaintext no longer matches anything.
    r = await client.request("DELETE", "/api/auth/mfa", json={"code": spent}, headers=auth(token))
    assert r.status_code == 200

    again = await _enroll_totp(client, token)
    await _confirm(client, token, again["secret"])
    assert spent not in again["recovery_codes"]
    r = await client.request("DELETE", "/api/auth/mfa", json={"code": spent}, headers=auth(token))
    assert r.status_code == 400


# ---- storage -------------------------------------------------------------------------------
async def test_nothing_reversible_reaches_the_database(client):
    """The seed is sealed and recovery codes are hashed — a dump of these tables is inert."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.mfa import MFARecoveryCode, UserMFA

    token = await signup(client, slug="mfa-store", email="a@mfa-store.com")
    body = await _enroll_totp(client, token)
    await _confirm(client, token, body["secret"])

    async with get_sessionmaker()() as s:
        factor = (await s.scalars(select(UserMFA))).one()
        assert factor.secret and factor.secret != body["secret"]
        assert body["secret"] not in factor.secret
        # The replay counter was recorded by the confirm — without it the guard is decorative.
        assert factor.last_used_counter is not None
        assert factor.confirmed_at is not None

        rows = (await s.scalars(select(MFARecoveryCode))).all()
        assert len(rows) == get_settings().mfa_recovery_code_count
        stored = {r.code_hash for r in rows}
        for plaintext in body["recovery_codes"]:
            assert plaintext not in stored
            assert not any(plaintext in h for h in stored)
