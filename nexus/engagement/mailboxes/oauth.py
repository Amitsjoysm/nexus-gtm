"""OAuth for connecting an SDR's own mailbox: authorize URL, code exchange, refresh (D1, D2).

The state round-tripped through Google or Microsoft is a short-lived signed JWT carrying the user,
tenant, provider and the browser's timezone, so the callback resumes with no server-side session
store. Its ``typ`` differs from the network connectors' state, so a state minted for one flow is
refused by the other.

**The state is not enough on its own, and was the whole credential until 2026-11-20.** It travels
in the consent link, so anyone who pressed Connect could send that link to somebody else, and when
that person signed in and clicked Allow their mailbox (read every message, send as them) joined the
first person's workspace. So the browser that presses Connect also gets a ``binding`` cookie: a
second signed JWT holding a random nonce and the PKCE verifier, HttpOnly and scoped to the OAuth
path. The state carries only the nonce's hash, and the callback goes ahead only when the browser it
lands in holds the matching cookie (`binding_verifier`). The verifier left the link at the same
time: it is what makes a stolen code useless, and a link is a poor place to keep it.

Token requests go through ``post_token``, which maps the two refusals that matter:

* ``invalid_grant`` on a refresh — the refresh token is revoked or expired → ``AuthExpired``; the
  mailbox needs the SDR to reconnect.
* anything else 4xx → ``ProviderError`` with the provider's words.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
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
_BINDING_TYP = "mailbox_oauth_browser"
_STATE_TTL_S = 600
#: Where the binding cookie is sent: the start and callback routes, nothing else.
BINDING_COOKIE_PATH = "/api/engagement/mailboxes/oauth"
BINDING_MAX_AGE_S = _STATE_TTL_S
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


def microsoft_authorize(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/authorize"


def microsoft_token(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/token"


def binding_cookie_name(provider: str) -> str:
    """One per provider, so connecting Gmail and Outlook in two tabs does not cancel either."""
    return f"nexus_mailbox_{provider}"


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256((nonce or "").encode()).hexdigest()


def _signed(claims: dict) -> str:
    s = get_settings()
    now = utcnow()
    claims = {**claims, "iat": int(now.timestamp()),
              "exp": int((now + timedelta(seconds=_STATE_TTL_S)).timestamp())}
    return jwt.encode(claims, s.secret_key, algorithm=s.jwt_algorithm)


def _decoded(token: str, typ: str, provider: str) -> dict | None:
    s = get_settings()
    try:
        claims = jwt.decode(token or "", s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if claims.get("typ") != typ or claims.get("prov") != provider:
        return None
    return claims


def sign_state(*, user_id: str, tenant_id: str, provider: str, nonce_hash: str,
               timezone: str) -> str:
    return _signed({"typ": _STATE_TYP, "uid": user_id, "tid": tenant_id, "prov": provider,
                    "nh": nonce_hash, "tz": (timezone or "UTC")[:64]})


def verify_state(token: str, *, provider: str) -> dict | None:
    claims = _decoded(token, _STATE_TYP, provider)
    if claims is None or not (claims.get("uid") and claims.get("tid") and claims.get("nh")):
        return None
    return claims


def sign_binding(*, user_id: str, tenant_id: str, provider: str, nonce: str, verifier: str) -> str:
    return _signed({"typ": _BINDING_TYP, "uid": user_id, "tid": tenant_id, "prov": provider,
                    "n": nonce, "pkce": verifier})


def binding_verifier(cookie: str | None, state_claims: dict, *, provider: str) -> str | None:
    """The PKCE verifier, when this browser is the one that started the flow ``state_claims``
    describes; ``None`` otherwise. Same user, workspace and provider, and the nonce whose hash the
    state carries."""
    claims = _decoded(cookie or "", _BINDING_TYP, provider)
    if claims is None or not claims.get("pkce") or not claims.get("n"):
        return None
    if (claims.get("uid"), claims.get("tid")) != (state_claims.get("uid"),
                                                  state_claims.get("tid")):
        return None
    if not hmac.compare_digest(_nonce_hash(claims["n"]), str(state_claims.get("nh") or "")):
        return None
    return str(claims["pkce"])


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


def start(app: OAuthApp, *, user_id: str, tenant_id: str, timezone: str) -> tuple[str, str]:
    """``(authorize_url, binding)``: a ready-to-open authorize URL with fresh PKCE and a signed
    state, and the value of the cookie that ties it to the browser asking."""
    verifier, challenge = make_pkce()
    nonce = secrets.token_urlsafe(32)
    state = sign_state(user_id=user_id, tenant_id=tenant_id, provider=app.provider,
                       nonce_hash=_nonce_hash(nonce), timezone=timezone)
    binding = sign_binding(user_id=user_id, tenant_id=tenant_id, provider=app.provider,
                           nonce=nonce, verifier=verifier)
    return authorize_url(app, state=state, challenge=challenge), binding


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
