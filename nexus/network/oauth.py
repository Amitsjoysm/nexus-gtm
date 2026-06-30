# nexus/network/oauth.py
"""OAuth helpers for the network connectors: PKCE and a signed, short-TTL state token.

The ``state`` round-tripped through the provider is a JWT (reusing the app's ``secret_key`` /
``jose``) carrying the member, tenant, provider, and the PKCE verifier — so the callback can resume
the flow with no server-side session store, and a tampered/expired/foreign token is rejected.
"""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta

from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow

_STATE_TYP = "net_oauth"
_DEFAULT_TTL_S = 600


def make_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def sign_state(
    *, member_id: str, tenant_id: str, provider: str, verifier: str, ttl_s: int = _DEFAULT_TTL_S
) -> str:
    s = get_settings()
    now = utcnow()
    # Stateless by design (no jti / server-side store): the bound authorization code is single-use at
    # the provider's token endpoint, so a replayed state carrying a consumed code fails there.
    payload = {
        "typ": _STATE_TYP, "mid": member_id, "tid": tenant_id, "prov": provider, "pkce": verifier,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
    }
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def verify_state(token: str) -> dict | None:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    return claims if claims.get("typ") == _STATE_TYP else None
