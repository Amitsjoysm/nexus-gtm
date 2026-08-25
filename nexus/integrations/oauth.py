# nexus/integrations/oauth.py
"""OAuth for the CRM/SEP integrations: authorize URLs, code exchange, and refresh.

Mirrors :mod:`nexus.network.oauth` deliberately — same signed short-TTL state JWT carrying the
tenant and provider, so the callback resumes the flow with no server-side session store and a
tampered, expired, or foreign state is rejected. The claim ``typ`` differs (``int_oauth``), which
is what stops a network-connector state from being replayed against a CRM callback.

**Not every provider supports PKCE.** HubSpot and Salesforce authenticate the token exchange with
``client_secret``; Salesforce additionally accepts PKCE. The verifier is therefore optional and
carried in the state when present. The signed state is what prevents CSRF in either case — it is
not decorative, and the callback must refuse a request whose state does not verify.

Everything is **inert without configuration**: with no client id/secret, :func:`provider_configured`
is false and the endpoints report that plainly rather than building an authorize URL the vendor
would reject with an opaque error.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.integrations.settings import get_integration_settings

logger = logging.getLogger("nexus.integrations.oauth")

_STATE_TYP = "int_oauth"
_DEFAULT_TTL_S = 600

# Access tokens are refreshed this many seconds before they actually expire, so a request that
# takes a moment to reach the vendor does not arrive with a token that expired in flight.
_REFRESH_SKEW_S = 120

_AUTHORIZE_URLS = {
    "hubspot": "https://app.hubspot.com/oauth/authorize",
    "outreach": "https://api.outreach.io/oauth/authorize",
}
_TOKEN_URLS = {
    "hubspot": "https://api.hubapi.com/oauth/v1/token",
    "outreach": "https://api.outreach.io/oauth/token",
}

# Least privilege: exactly what the connectors call, nothing more. Widening a scope list forces
# every existing customer through a re-consent screen, so these are chosen once and deliberately.
_SCOPES = {
    "hubspot": "crm.objects.companies.read crm.objects.companies.write "
               "crm.objects.contacts.read crm.objects.contacts.write",
    "salesforce": "api refresh_token",
    "outreach": "prospects.all sequences.all sequenceStates.all",
}


def make_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _client(provider: str) -> tuple[str, str]:
    s = get_integration_settings()
    if provider == "hubspot":
        return s.hubspot_client_id, s.hubspot_client_secret
    if provider == "salesforce":
        return s.salesforce_client_id, s.salesforce_client_secret
    if provider == "outreach":
        return s.outreach_client_id, s.outreach_client_secret
    return "", ""


def provider_configured(provider: str) -> bool:
    """Whether this deployment has an app registration for ``provider``."""
    cid, secret = _client(provider)
    return bool(cid and secret and get_integration_settings().oauth_redirect_base)


def redirect_uri(kind: str, provider: str) -> str:
    """The callback URL registered with the vendor. Must match their console exactly."""
    base = get_integration_settings().oauth_redirect_base.rstrip("/")
    return f"{base}/api/integrations/{kind}/oauth/{provider}/callback"


def _authorize_url(provider: str) -> str:
    if provider == "salesforce":
        base = get_integration_settings().salesforce_login_base.rstrip("/")
        return f"{base}/services/oauth2/authorize"
    return _AUTHORIZE_URLS.get(provider, "")


def _token_url(provider: str) -> str:
    if provider == "salesforce":
        base = get_integration_settings().salesforce_login_base.rstrip("/")
        return f"{base}/services/oauth2/token"
    return _TOKEN_URLS.get(provider, "")


def sign_state(
    *, tenant_id: str, user_id: str, kind: str, provider: str,
    verifier: str = "", ttl_s: int = _DEFAULT_TTL_S,
) -> str:
    """A short-TTL JWT binding the flow to this tenant, user, kind, and provider.

    Stateless by design (no jti / server-side store): the bound authorization code is single-use at
    the provider's token endpoint, so a replayed state carrying a consumed code fails there.
    """
    s = get_settings()
    now = utcnow()
    payload = {
        "typ": _STATE_TYP, "tid": tenant_id, "uid": user_id, "kind": kind, "prov": provider,
        "pkce": verifier,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
    }
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def verify_state(token: str) -> dict | None:
    """Decode a state token, or ``None`` if it is tampered, expired, or not one of ours."""
    s = get_settings()
    try:
        claims = jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    return claims if claims.get("typ") == _STATE_TYP else None


def authorize_url(*, kind: str, provider: str, state: str, challenge: str = "") -> str:
    """The vendor URL to send the admin to."""
    cid, _ = _client(provider)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(kind, provider),
        "response_type": "code",
        "state": state,
    }
    scope = _SCOPES.get(provider, "")
    if scope:
        params["scope"] = scope
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return f"{_authorize_url(provider)}?{urllib.parse.urlencode(params)}"


def _post_form_blocking(url: str, form: dict) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}


async def _post_form(url: str, form: dict) -> tuple[int, dict]:
    return await asyncio.to_thread(_post_form_blocking, url, form)


def _bundle_from_token_response(body: dict) -> dict:
    """Normalise a vendor token response into the bundle we seal.

    ``expires_at`` is stored as an absolute epoch second rather than the vendor's relative
    ``expires_in``, because the bundle outlives the response by definition — a relative lifetime
    read back an hour later means nothing.
    """
    bundle = {
        "access_token": body.get("access_token", ""),
        "refresh_token": body.get("refresh_token", ""),
    }
    expires_in = body.get("expires_in")
    if expires_in:
        try:
            bundle["expires_at"] = int(utcnow().timestamp()) + int(expires_in)
        except (TypeError, ValueError):
            pass
    # Salesforce addresses REST at the org's own host, returned only here.
    if body.get("instance_url"):
        bundle["instance_url"] = body["instance_url"]
    return {k: v for k, v in bundle.items() if v}


async def exchange_code(*, kind: str, provider: str, code: str, verifier: str = "") -> dict:
    """Trade an authorization code for a token bundle. ``{}`` on any failure."""
    cid, secret = _client(provider)
    form = {
        "grant_type": "authorization_code",
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect_uri(kind, provider),
        "code": code,
    }
    if verifier:
        form["code_verifier"] = verifier
    status, body = await _post_form(_token_url(provider), form)
    if status != 200 or not body.get("access_token"):
        # The vendor's error text can name the misconfiguration (bad redirect_uri, wrong secret),
        # which is the one thing an operator needs; it carries no token.
        logger.warning("[oauth] %s code exchange failed: %s %s", provider, status,
                       body.get("error_description") or body.get("error") or "")
        return {}
    return _bundle_from_token_response(body)


def needs_refresh(bundle: dict) -> bool:
    """Whether the access token is missing or close enough to expiry to renew now."""
    if not bundle.get("access_token"):
        return bool(bundle.get("refresh_token"))
    expires_at = bundle.get("expires_at")
    if not expires_at:
        return False  # a pasted private-app token has no expiry and never needs refreshing
    try:
        return int(utcnow().timestamp()) >= int(expires_at) - _REFRESH_SKEW_S
    except (TypeError, ValueError):
        return False


async def refresh_access_token(provider: str, bundle: dict) -> dict:
    """Exchange the stored refresh token for a fresh access token.

    Returns only the **changed** fields, so the caller merges rather than replaces — HubSpot does
    not return the refresh token on a refresh, and replacing the bundle wholesale would delete the
    very credential that makes the connection durable.
    """
    refresh = bundle.get("refresh_token")
    if not refresh:
        return {}
    cid, secret = _client(provider)
    status, body = await _post_form(_token_url(provider), {
        "grant_type": "refresh_token",
        "client_id": cid,
        "client_secret": secret,
        "refresh_token": refresh,
    })
    if status != 200 or not body.get("access_token"):
        logger.warning("[oauth] %s refresh failed: %s %s", provider, status,
                       body.get("error_description") or body.get("error") or "")
        return {}
    # _bundle_from_token_response already drops empty values, so a response without a refresh
    # token simply omits the key and the caller's merge keeps the stored one.
    return _bundle_from_token_response(body)
