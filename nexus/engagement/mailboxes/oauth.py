"""OAuth for connecting an SDR's own mailbox: authorize URL, code exchange, refresh (D1, D2).

The state round-tripped through Google or Microsoft is a short-lived signed JWT carrying the user,
tenant, provider, PKCE verifier and the browser's timezone, so the callback resumes with no
server-side session store. Its ``typ`` differs from the network connectors' state, so a state minted
for one flow is refused by the other.

Token requests go through ``post_token``, which maps the two refusals that matter:

* ``invalid_grant`` on a refresh — the refresh token is revoked or expired → ``AuthExpired``; the
  mailbox needs the SDR to reconnect.
* anything else 4xx → ``ProviderError`` with the provider's words.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.engagement.config import GOOGLE, MICROSOFT, OAuthApp
from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError, TransientError
from nexus.network.oauth import make_pkce

_STATE_TYP = "mailbox_oauth"
_STATE_TTL_S = 600
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


def microsoft_authorize(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/authorize"


def microsoft_token(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/token"


def sign_state(*, user_id: str, tenant_id: str, provider: str, verifier: str, timezone: str) -> str:
    s = get_settings()
    now = utcnow()
    claims = {
        "typ": _STATE_TYP, "uid": user_id, "tid": tenant_id, "prov": provider, "pkce": verifier,
        "tz": (timezone or "UTC")[:64],
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=_STATE_TTL_S)).timestamp()),
    }
    return jwt.encode(claims, s.secret_key, algorithm=s.jwt_algorithm)


def verify_state(token: str, *, provider: str) -> dict | None:
    s = get_settings()
    try:
        claims = jwt.decode(token or "", s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if claims.get("typ") != _STATE_TYP or claims.get("prov") != provider:
        return None
    if not (claims.get("uid") and claims.get("tid") and claims.get("pkce")):
        return None
    return claims


def authorize_url(app: OAuthApp, *, state: str, challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": app.client_id, "redirect_uri": app.redirect_uri, "response_type": "code",
        "scope": " ".join(app.scopes), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if app.provider == GOOGLE:
        # offline + consent: a refresh token is issued on every connect, not only the first.
        params.update({"access_type": "offline", "prompt": "consent"})
        base = GOOGLE_AUTHORIZE
    elif app.provider == MICROSOFT:
        params.update({"response_mode": "query", "prompt": "select_account"})
        base = microsoft_authorize(app.tenant)
    else:
        raise ValueError(f"unknown mailbox provider {app.provider!r}")
    if login_hint:
        params["login_hint"] = login_hint
    return f"{base}?{urlencode(params)}"


def start(app: OAuthApp, *, user_id: str, tenant_id: str, timezone: str) -> str:
    """A ready-to-open authorize URL with fresh PKCE and a signed state."""
    verifier, challenge = make_pkce()
    state = sign_state(user_id=user_id, tenant_id=tenant_id, provider=app.provider,
                       verifier=verifier, timezone=timezone)
    return authorize_url(app, state=state, challenge=challenge)


def missing_scopes(app: OAuthApp, granted: str) -> list[str]:
    """Scopes we asked for that the grant does not include. A user can untick Gmail access on
    Google's consent screen; a mailbox without it would connect and then fail every sync."""
    have = {s.strip().lower() for s in (granted or "").split() if s.strip()}
    # Microsoft reports Graph scopes without the resource prefix ("Mail.Send"); openid/email may be
    # omitted from the granted list even when granted.
    have |= {f"https://graph.microsoft.com/{s}" for s in list(have) if "/" not in s}
    required = [s for s in app.scopes if s not in ("openid", "email", "offline_access")]
    return [s for s in required if s.lower() not in have]


def bundle_from_response(data: dict, *, previous: dict | None = None) -> dict:
    """The token bundle to seal. Google omits ``refresh_token`` on refresh, so the old one is kept;
    Microsoft rotates it, so the new one wins."""
    previous = previous or {}
    now = int(time.time())
    return {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
        "expires_at": now + int(data.get("expires_in") or 3600),
        "scope": data.get("scope") or previous.get("scope", ""),
        "token_type": data.get("token_type", "Bearer"),
        "refreshed_at": now,
    }


def token_url(app: OAuthApp) -> str:
    return GOOGLE_TOKEN if app.provider == GOOGLE else microsoft_token(app.tenant)


async def post_token(app: OAuthApp, form: dict) -> dict:
    data = {"client_id": app.client_id, "client_secret": app.client_secret, **form}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(token_url(app), data=data)
    except httpx.HTTPError as exc:
        raise TransientError(f"{type(exc).__name__}: could not reach the token endpoint") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 500:
        raise TransientError(resp.text[:300], status=resp.status_code)
    if resp.status_code >= 400:
        detail = str(body.get("error_description") or body.get("error") or resp.text)[:300]
        if body.get("error") == "invalid_grant" and form.get("grant_type") == "refresh_token":
            raise AuthExpired(detail, status=resp.status_code)
        raise ProviderError(detail, status=resp.status_code)
    return body


async def exchange_code(app: OAuthApp, *, code: str, verifier: str) -> dict:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": app.redirect_uri,
            "code_verifier": verifier}
    if app.provider == MICROSOFT:
        form["scope"] = " ".join(app.scopes)
    return await post_token(app, form)


async def refresh(app: OAuthApp, refresh_token: str) -> dict:
    if not refresh_token:
        raise AuthExpired("no refresh token is stored for this mailbox")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if app.provider == MICROSOFT:
        form["scope"] = " ".join(app.scopes)
    return await post_token(app, form)


async def revoke(app: OAuthApp, bundle: dict) -> None:
    """Best effort. Google revokes the grant; Microsoft has no per-app revoke for delegated tokens
    short of signing the user out everywhere, so locally deleting the tokens is the disconnect."""
    token = bundle.get("refresh_token") or bundle.get("access_token")
    if app.provider != GOOGLE or not token:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(GOOGLE_REVOKE, data={"token": token})
    except httpx.HTTPError:
        return
