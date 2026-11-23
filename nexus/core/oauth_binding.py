"""Tie an OAuth consent link to the browser that pressed Connect (security fix, 2026-11-20).

Every OAuth flow here round-trips a signed ``state`` through the provider, and that state used to
be the whole credential: whoever pressed Connect could send the provider's consent link to someone
else, and when that person signed in and clicked Allow, THEIR account (a CRM, a calendar and
contacts) was stored in the first person's workspace. The state proves which workspace started the
flow; nothing proved the person finishing it was the same browser.

So Start also sets a binding cookie, a second signed JWT holding a random nonce, the PKCE verifier
and the claims the state carries. It is HttpOnly (page scripts cannot read it), SameSite=Lax (it
rides the top-level redirect back from the provider and no cross-site subrequest), scoped to the
flow's own path and as short-lived as the state. The state carries only the nonce's sha256 and no
verifier. The callback goes ahead only when the browser it lands in holds a cookie whose nonce
hashes to the state's and whose claims match it, and the caller deletes the cookie on every
outcome, so one Connect press is one callback.

`engagement/mailboxes/oauth.py` closed the same hole for mailboxes first, with its own copy of these
steps; the CRM (`integrations/oauth.py`) and network (`network/oauth.py`) flows use this one.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow

DEFAULT_TTL_S = 600


def nonce_hash(nonce: str) -> str:
    return hashlib.sha256((nonce or "").encode()).hexdigest()


def new_binding(*, typ: str, subject: dict, verifier: str,
                ttl_s: int = DEFAULT_TTL_S) -> tuple[str, str]:
    """``(nonce_hash, cookie)``: the hash goes into the state, the cookie to the browser.

    ``subject`` is what the state also says (tenant, user or member, provider, kind), checked again
    on the way back so a cookie from one flow cannot unlock another's state."""
    s = get_settings()
    now = utcnow()
    nonce = secrets.token_urlsafe(32)
    claims = {**subject, "typ": typ, "n": nonce, "pkce": verifier or "",
              "iat": int(now.timestamp()),
              "exp": int((now + timedelta(seconds=ttl_s)).timestamp())}
    return nonce_hash(nonce), jwt.encode(claims, s.secret_key, algorithm=s.jwt_algorithm)


def bound_verifier(cookie: str | None, *, typ: str, state_nonce_hash: str,
                   subject: dict) -> str | None:
    """The PKCE verifier ("" when the flow has none) when ``cookie`` is this browser's binding for
    the state described by ``state_nonce_hash`` and ``subject``; ``None`` otherwise."""
    if not cookie or not state_nonce_hash:
        return None
    s = get_settings()
    try:
        claims = jwt.decode(cookie, s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if claims.get("typ") != typ or not claims.get("n"):
        return None
    if any(str(claims.get(key) or "") != str(value or "") for key, value in subject.items()):
        return None
    if not hmac.compare_digest(nonce_hash(str(claims["n"])), str(state_nonce_hash)):
        return None
    return str(claims.get("pkce") or "")


def set_cookie(response, request, *, name: str, value: str, path: str,
               ttl_s: int = DEFAULT_TTL_S) -> None:
    """Secure whenever the request arrived over https (uvicorn runs with --proxy-headers, so a TLS
    proxy in front still reads as https); a local http stack keeps working."""
    response.set_cookie(name, value, max_age=ttl_s, path=path, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")


def clear_cookie(response, *, name: str, path: str) -> None:
    response.delete_cookie(name, path=path)
