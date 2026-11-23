# nexus/network/oauth.py
"""OAuth helpers for the network connectors: PKCE, a signed short-TTL state, and the browser binding.

The ``state`` round-tripped through the provider is a JWT (reusing the app's ``secret_key`` /
``jose``) carrying the member, tenant and provider, so the callback can resume the flow with no
server-side session store, and a tampered/expired/foreign token is rejected.

Since 2026-11-20 it is not the whole credential: it travels in the consent link, and anyone who
sent theirs to someone else received that person's contacts and calendar. Start also sets a binding
cookie (`core/oauth_binding`, `binding_cookie`) holding a nonce and the PKCE verifier; the state
carries only the nonce's hash (``nh``) and no verifier, and the callback exchanges nothing unless
the browser it lands in holds the matching cookie.
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
_BINDING_TYP = "net_oauth_browser"
_DEFAULT_TTL_S = 600


def binding_cookie(provider: str) -> tuple[str, str]:
    """``(name, path)`` of the cookie tying one provider's flow to its browser."""
    return f"nexus_oauth_net_{provider}", f"/api/network/oauth/{provider}"


def new_binding(*, member_id: str, tenant_id: str, provider: str,
                verifier: str) -> tuple[str, str]:
    """``(nonce_hash, cookie value)`` for a Start: the hash goes into the state."""
    from nexus.core.oauth_binding import new_binding as _new

    return _new(typ=_BINDING_TYP, verifier=verifier,
                subject={"mid": member_id, "tid": tenant_id, "prov": provider})


def bound_verifier(cookie: str | None, state_claims: dict) -> str | None:
    """The PKCE verifier when ``cookie`` is the binding this state's Start set in this browser;
    ``None`` otherwise, and nothing may be exchanged."""
    from nexus.core.oauth_binding import bound_verifier as _bound

    return _bound(cookie, typ=_BINDING_TYP, state_nonce_hash=str(state_claims.get("nh") or ""),
                  subject={key: state_claims.get(key) for key in ("mid", "tid", "prov")})


def make_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def sign_state(
    *, member_id: str, tenant_id: str, provider: str, nonce_hash: str,
    ttl_s: int = _DEFAULT_TTL_S,
) -> str:
    s = get_settings()
    now = utcnow()
    # Stateless by design (no jti / server-side store): the bound authorization code is single-use at
    # the provider's token endpoint, so a replayed state carrying a consumed code fails there.
    payload = {
        "typ": _STATE_TYP, "mid": member_id, "tid": tenant_id, "prov": provider, "nh": nonce_hash,
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
