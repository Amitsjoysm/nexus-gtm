"""Password hashing and JWT issuance/verification.

Two kinds of token are signed with the same key, so they are told apart by a ``typ`` claim — the
convention ``nexus/network/oauth.py`` already uses for its PKCE state:

* the **access token** carries no ``typ`` (unchanged from before MFA existed, so tokens issued by
  the previous release keep working across a rolling deploy);
* the **MFA challenge** carries ``typ="mfa_challenge"`` and authorizes exactly one thing —
  exchanging a second-factor code for a real access token.

:func:`decode_access_token` fails **closed** on any ``typ`` it does not recognise. That is what
stops a challenge token being presented as a bearer token: it is a signed, unexpired JWT with a
valid ``sub``/``tid``, so without this check it would have authenticated every endpoint in the
API and MFA would have been a formality.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from nexus.core.config import get_settings
from nexus.core.db import utcnow

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Absent on access tokens by design — see the module docstring.
ACCESS_TYP = "access"
MFA_CHALLENGE_TYP = "mfa_challenge"


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(
    *, user_id: str, tenant_id: str, role: str, token_version: int | None = None
) -> str:
    settings = get_settings()
    now = utcnow()
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_min)).timestamp()),
    }
    # Omitted when the caller does not know it, so a token minted by a path that has not been
    # taught about revocation still works rather than being born invalid.
    if token_version is not None:
        payload["tv"] = int(token_version)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def _decode(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def decode_access_token(token: str) -> dict | None:
    """Decode a bearer token, refusing anything that is not an access token.

    The ``typ`` test is written as an allowlist (``None`` or ``"access"``) rather than a denylist
    so that every future token kind is rejected here until someone deliberately admits it.
    """
    payload = _decode(token)
    if payload is None:
        return None
    if payload.get("typ") not in (None, ACCESS_TYP):
        return None
    return payload


def create_impersonation_token(
    *, user_id: str, tenant_id: str, role: str, impersonator_id: str, ttl_min: int = 30
) -> str:
    """A short-lived, **read-only** credential that lets a platform admin see what a user sees.

    Deliberately an ``access`` token rather than a new type: an admin needs to browse the real
    application, and a bespoke type would mean auditing every endpoint for a second code path — the
    surest way to leave one out.

    What makes it safe is not the type but three claims:

    * ``imp`` names the impersonator, so every request is attributable to a real person. An
      impersonation session that cannot be traced back to a human is indistinguishable from a
      compromised account.
    * ``ro`` marks it read-only. ``require_writable`` refuses every mutating request carrying it —
      support diagnosing a problem never needs to change the customer's data, and an admin acting
      unnoticed inside a customer account is the single worst failure mode this feature has.
    * ``exp`` is minutes, not hours. Time-boxing is the difference between a support session and a
      standing key to every account.
    """
    settings = get_settings()
    now = utcnow()
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "typ": ACCESS_TYP,
        "imp": impersonator_id,
        "ro": True,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=max(1, ttl_min))).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_mfa_challenge_token(*, user_id: str, tenant_id: str, role: str) -> str:
    """A short-TTL credential that proves 'this password was correct' and nothing else.

    It is not an access token: :func:`decode_access_token` rejects it, so it cannot be used as a
    bearer token anywhere in the API. Its only accepted use is ``POST /auth/mfa/verify``.
    """
    settings = get_settings()
    now = utcnow()
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "typ": MFA_CHALLENGE_TYP,
        # Distinct per challenge, so two concurrent logins are distinguishable in logs.
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.mfa_challenge_ttl_s)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_mfa_challenge_token(token: str) -> dict | None:
    """Decode an MFA challenge, refusing an access token presented in its place."""
    payload = _decode(token)
    if payload is None or payload.get("typ") != MFA_CHALLENGE_TYP:
        return None
    if "sub" not in payload or "tid" not in payload:
        return None
    return payload
