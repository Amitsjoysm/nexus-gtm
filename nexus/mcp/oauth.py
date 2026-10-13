# nexus/mcp/oauth.py
"""NEXUS's OAuth 2.1 authorization server, for AI apps connecting through MCP.

Claude (every surface), ChatGPT, VS Code and Cursor connect by OAuth: the user signs in to NEXUS,
ticks what the app may do, and the app receives tokens. What this module holds to, and why:

* **Three ways for an app to identify itself**, in the spec's priority order: a client a superadmin
  registered (with a secret; covers Anthropic-held credentials), a Client ID Metadata Document (an
  https URL that serves the app's metadata; ``cimd.py``), and Dynamic Client Registration (kept for
  older clients although the 2026-07-28 revision deprecates it).
* **An unknown client or an unregistered redirect is an error page, never a redirect.** Redirecting
  an error to an address we cannot vouch for is an open redirector.
* **PKCE S256 is required** on every authorization, and the code is single-use, 60 seconds, bound to
  its client, redirect and challenge. A code presented twice revokes what it issued.
* **Loopback redirects match on any port** (``localhost``, ``127.0.0.1``, ``[::1]``): Claude Code
  and other native apps listen on an ephemeral port (RFC 8252 §7.3).
* **Refresh tokens rotate, and reuse revokes the family** (OAuth 2.1 for public clients).
* **Tokens are opaque and stored hashed**, so revoking a connection takes effect on the next call.
* **The consent screen names the redirect host**, and an app known only by what it says about
  itself is labelled Unverified: anyone can register a client called "Claude".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from jose import JWTError, jwt
from sqlalchemy import select, update

from nexus.core.db import get_sessionmaker, utcnow
from nexus.mcp import credentials
from nexus.mcp.credentials import ACCESS_PREFIX, OAUTH_SCOPE, REFRESH_PREFIX, SCOPE_FROM_OAUTH
from nexus.models.mcp import McpConnection, OAuthClient, OAuthCode, OAuthToken
from nexus.mcp.brand import PRODUCT_NAME

REQUEST_TYP = "oauth_request"
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(seconds=60)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
#: CIMD documents served from these hosts (or their subdomains) are labelled Verified: the host
#: proves who the app is, which a self-declared name cannot.
VERIFIED_HOSTS = ("claude.ai", "anthropic.com", "chatgpt.com", "openai.com")
SUPPORTED_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


class OAuthError(Exception):
    """An RFC 6749 error. ``redirect`` says whether it may be sent to the client's redirect URI
    (only once the client and the redirect are known good)."""

    def __init__(self, error: str, description: str, *, status: int = 400,
                 redirect: bool = False) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.redirect = redirect

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


# ---- small rules -----------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def valid_redirect(uri: str) -> bool:
    """A redirect an app may register: absolute https, or http on a loopback address; no fragment."""
    try:
        parsed = urlparse(uri or "")
    except ValueError:
        return False
    if parsed.fragment or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and _is_loopback(parsed.hostname)


def redirect_matches(registered: list[str], presented: str) -> bool:
    """Exact match, or for http loopback the same scheme, host and path on any port."""
    if not presented:
        return False
    if presented in registered:
        return True
    try:
        p = urlparse(presented)
    except ValueError:
        return False
    if p.scheme != "http" or not _is_loopback(p.hostname or ""):
        return False
    for uri in registered:
        r = urlparse(uri)
        if (r.scheme == "http" and _is_loopback(r.hostname or "")
                and (r.hostname or "").strip("[]").lower() == (p.hostname or "").strip("[]").lower()
                and (r.path or "/") == (p.path or "/") and r.query == p.query):
            return True
    return False


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge or not 43 <= len(verifier) <= 128:
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "ignore"))
                                        .digest()).decode().rstrip("=")
    return hmac.compare_digest(computed, challenge)


def client_host(client: OAuthClient) -> str:
    for uri in client.redirect_uris or []:
        host = urlparse(uri).hostname
        if host:
            return host
    return urlparse(client.client_uri or client.client_id).hostname or ""


def only_loopback(client: OAuthClient) -> bool:
    uris = client.redirect_uris or []
    return bool(uris) and all(_is_loopback(urlparse(u).hostname or "") for u in uris)


def _verified_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in VERIFIED_HOSTS)


def is_verified(client: OAuthClient) -> bool:
    """Verified when something other than the app's own say-so proves who it is: a superadmin
    registered it, its metadata document is served from a known host, or every address it may
    return to is https on a known host. The last holds for a self-registered client too, because a
    code is only ever delivered to a registered redirect: a stranger naming itself "Claude" with
    claude.ai's callback could never receive one."""
    if client.kind == "registered":
        return True
    if client.kind == "cimd" and _verified_host(urlparse(client.client_id).hostname or ""):
        return True
    uris = client.redirect_uris or []
    return bool(uris) and all(
        urlparse(u).scheme == "https" and _verified_host(urlparse(u).hostname or "") for u in uris)


def parse_scope(raw: str | None) -> list[str]:
    """``nexus.read nexus.write offline_access`` -> ``["read", "write"]``. Nothing asked for means
    everything (the consent screen still lets the user untick). An unknown scope is an error."""
    words = [w for w in (raw or "").split() if w]
    if not words:
        return list(credentials.SCOPES)
    wanted: set[str] = set()
    for w in words:
        if w in ("offline_access", "openid", "profile", "email"):
            continue
        if w not in SCOPE_FROM_OAUTH:
            raise OAuthError("invalid_scope", f"unknown scope: {w}", redirect=True)
        wanted.add(SCOPE_FROM_OAUTH[w])
    return credentials.clean_scopes(wanted) or list(credentials.SCOPES)


def scope_string(scopes) -> str:
    return " ".join(OAUTH_SCOPE[s] for s in credentials.clean_scopes(scopes))


# ---- metadata --------------------------------------------------------------------------------

def authorization_server_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(SUPPORTED_AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": list(SUPPORTED_AUTH_METHODS),
        "scopes_supported": [OAUTH_SCOPE[s] for s in credentials.SCOPES] + ["offline_access"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{base}/connected-apps",
    }


def protected_resource_metadata(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": [OAUTH_SCOPE[s] for s in credentials.SCOPES],
        "bearer_methods_supported": ["header"],
        "resource_name": PRODUCT_NAME,
        "resource_documentation": f"{base}/connected-apps",
    }


# ---- clients ---------------------------------------------------------------------------------

async def register_client(body: dict) -> dict:
    """Dynamic Client Registration (RFC 7591). Public clients get no secret."""
    if not isinstance(body, dict):
        raise OAuthError("invalid_client_metadata", "the registration must be a JSON object")
    redirects = body.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or len(redirects) > 20:
        raise OAuthError("invalid_redirect_uri", "give between 1 and 20 redirect_uris")
    for uri in redirects:
        if not isinstance(uri, str) or not valid_redirect(uri):
            raise OAuthError("invalid_redirect_uri",
                             f"redirect URI not allowed (https, or http on loopback): {uri}")
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in SUPPORTED_AUTH_METHODS:
        raise OAuthError("invalid_client_metadata",
                         f"token_endpoint_auth_method must be one of {list(SUPPORTED_AUTH_METHODS)}")
    grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
    if not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise OAuthError("invalid_client_metadata",
                         "only authorization_code and refresh_token are supported")
    name = str(body.get("client_name") or "").strip()[:200] or "Unnamed app"
    client_id = "dcr_" + uuid.uuid4().hex
    secret = credentials.new_secret("nxs_") if method != "none" else ""
    async with get_sessionmaker()() as s:
        s.add(OAuthClient(
            client_id=client_id, kind="dcr", client_name=name,
            client_uri=str(body.get("client_uri") or "")[:512],
            logo_uri=str(body.get("logo_uri") or "")[:512], redirect_uris=list(redirects),
            token_endpoint_auth_method=method,
            client_secret_digest=credentials.digest(secret) if secret else "",
        ))
        await s.commit()
    out = {
        "client_id": client_id, "client_name": name, "redirect_uris": list(redirects),
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_id_issued_at": int(utcnow().timestamp()),
    }
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return out


async def create_registered_client(name: str, redirect_uris: list[str]) -> tuple[OAuthClient, str]:
    """A confidential client a superadmin creates (e.g. for Anthropic-held credentials)."""
    for uri in redirect_uris:
        if not valid_redirect(uri):
            raise OAuthError("invalid_redirect_uri", f"redirect URI not allowed: {uri}")
    secret = credentials.new_secret("nxs_")
    client = OAuthClient(
        client_id="nxc_" + uuid.uuid4().hex, kind="registered", client_name=name.strip()[:200],
        redirect_uris=list(redirect_uris), token_endpoint_auth_method="client_secret_basic",
        client_secret_digest=credentials.digest(secret),
    )
    async with get_sessionmaker()() as s:
        s.add(client)
        await s.commit()
    return client, secret


async def resolve_client(client_id: str) -> OAuthClient:
    """The client row, fetching (or re-fetching) a Client ID Metadata Document when needed."""
    from nexus.mcp import cimd

    client_id = (client_id or "").strip()
    if not client_id or len(client_id) > 512:
        raise OAuthError("invalid_client", "unknown client", status=401)
    now = utcnow()
    async with get_sessionmaker()() as s:
        client = (await s.scalars(select(OAuthClient).where(
            OAuthClient.client_id == client_id))).first()
        fresh = client is not None and (
            client.kind != "cimd" or (client.metadata_expires_at or now) > now)
        if fresh:
            return client
        if not cimd.looks_like_cimd(client_id):
            raise OAuthError("invalid_client", "unknown client", status=401)
        try:
            meta = await cimd.fetch(client_id)
        except cimd.CimdError as exc:
            if client is not None:
                return client  # keep using the last good document while the host is unreachable
            raise OAuthError("invalid_client", str(exc), status=401) from exc
        if client is None:
            client = OAuthClient(client_id=client_id, kind="cimd")
            s.add(client)
        client.client_name = meta.client_name
        client.client_uri = meta.client_uri
        client.logo_uri = meta.logo_uri
        client.redirect_uris = meta.redirect_uris
        client.token_endpoint_auth_method = (
            meta.token_endpoint_auth_method if meta.token_endpoint_auth_method == "none"
            else "none")
        client.metadata_expires_at = now + timedelta(seconds=meta.cache_seconds)
        await s.commit()
        return client


# ---- authorization ---------------------------------------------------------------------------

def _secret() -> str:
    from nexus.core.config import get_settings

    return get_settings().secret_key


def _algorithm() -> str:
    from nexus.core.config import get_settings

    return get_settings().jwt_algorithm


def sign_request(data: dict) -> str:
    now = utcnow()
    payload = {**data, "typ": REQUEST_TYP, "iat": int(now.timestamp()),
               "exp": int((now + REQUEST_TTL).timestamp()), "jti": secrets.token_hex(8)}
    return jwt.encode(payload, _secret(), algorithm=_algorithm())


def read_request(token: str) -> dict:
    try:
        data = jwt.decode(token or "", _secret(), algorithms=[_algorithm()])
    except JWTError as exc:
        raise OAuthError("invalid_request", "This sign-in link has expired. Start again from the "
                         "app.") from exc
    if data.get("typ") != REQUEST_TYP:
        raise OAuthError("invalid_request", "This is not a sign-in request.")
    return data


def redirect_with(uri: str, params: dict) -> str:
    sep = "&" if urlparse(uri).query else "?"
    return f"{uri}{sep}{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"


@dataclass
class AuthorizeResult:
    consent_url: str = ""
    error_redirect: str = ""


async def start_authorization(params: dict, base: str) -> AuthorizeResult:
    """Validate an authorization request and send the user to the consent page, or say why not.

    Raises :class:`OAuthError` with ``redirect=False`` for problems that must be shown on our own
    error page; returns ``error_redirect`` for problems the client should hear about."""
    client = await resolve_client(params.get("client_id", ""))
    registered = list(client.redirect_uris or [])
    redirect_uri = params.get("redirect_uri") or ""
    if not redirect_uri:
        if len(registered) != 1:
            raise OAuthError("invalid_request", "redirect_uri is required")
        redirect_uri = registered[0]
    if not redirect_matches(registered, redirect_uri):
        raise OAuthError("invalid_request", "this redirect_uri is not registered for the app")
    state = params.get("state") or ""

    def fail(error: str, description: str) -> AuthorizeResult:
        return AuthorizeResult(error_redirect=redirect_with(redirect_uri, {
            "error": error, "error_description": description, "state": state, "iss": base}))

    if params.get("response_type") != "code":
        return fail("unsupported_response_type", "only response_type=code is supported")
    challenge = params.get("code_challenge") or ""
    if not challenge or params.get("code_challenge_method") != "S256":
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    if not 43 <= len(challenge) <= 128:
        return fail("invalid_request", "code_challenge is not a valid S256 challenge")
    resource = params.get("resource") or ""
    if resource and resource.rstrip("/") != f"{base}/mcp":
        return fail("invalid_target", "resource must be this server's MCP endpoint")
    try:
        scopes = parse_scope(params.get("scope"))
    except OAuthError as exc:
        return fail(exc.error, exc.description)
    request = sign_request({
        "client_id": client.client_id, "redirect_uri": redirect_uri, "scopes": scopes,
        "state": state, "code_challenge": challenge, "resource": resource,
    })
    return AuthorizeResult(consent_url=f"{base}/oauth/consent?{urlencode({'request': request})}")


async def request_view(token: str) -> dict:
    """What the consent screen shows."""
    data = read_request(token)
    client = await resolve_client(data["client_id"])
    return {
        "client_name": client.client_name or "Unnamed app",
        "client_uri": client.client_uri,
        "logo_uri": client.logo_uri if client.kind != "dcr" else "",
        "verified": is_verified(client),
        "redirect_host": urlparse(data["redirect_uri"]).hostname or "",
        "only_loopback": only_loopback(client),
        "scopes": data["scopes"],
    }


async def approve(token: str, *, user_id: str, tenant_id: str, scopes, base: str) -> str:
    """Create (or refresh) the connection and a code; the URL to send the browser to."""
    data = read_request(token)
    client = await resolve_client(data["client_id"])
    granted = [s for s in credentials.clean_scopes(scopes) if s in data["scopes"]]
    if not granted:
        raise OAuthError("invalid_scope", "choose at least one permission")
    code = secrets.token_urlsafe(32)
    now = utcnow()
    async with get_sessionmaker()() as s:
        conn = (await s.scalars(select(McpConnection).where(
            McpConnection.subject_tenant_id == tenant_id, McpConnection.user_id == user_id,
            McpConnection.kind == "oauth", McpConnection.client_id == client.client_id,
            McpConnection.revoked_at.is_(None)))).first()
        if conn is None:
            conn = McpConnection(subject_tenant_id=tenant_id, user_id=user_id, kind="oauth",
                                 client_id=client.client_id)
            s.add(conn)
        conn.name = client.client_name or "AI app"
        conn.scopes = granted
        await s.flush()
        s.add(OAuthCode(
            code_digest=credentials.digest(code), client_id=client.client_id,
            subject_tenant_id=tenant_id, user_id=user_id, connection_id=conn.id,
            redirect_uri=data["redirect_uri"], scopes=granted,
            code_challenge=data["code_challenge"], resource=data.get("resource") or "",
            expires_at=now + CODE_TTL,
        ))
        await s.commit()
        connection_id = conn.id
    credentials.forget(connection_id)
    return redirect_with(data["redirect_uri"], {"code": code, "state": data.get("state"),
                                                "iss": base})


def deny(token: str, base: str) -> str:
    data = read_request(token)
    return redirect_with(data["redirect_uri"], {
        "error": "access_denied", "error_description": "The user declined.",
        "state": data.get("state"), "iss": base})


# ---- tokens ----------------------------------------------------------------------------------

def _client_credentials(form: dict, authorization: str | None) -> tuple[str, str]:
    """(client_id, client_secret) from the form or an HTTP Basic header."""
    client_id = str(form.get("client_id") or "")
    client_secret = str(form.get("client_secret") or "")
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            from urllib.parse import unquote

            decoded = base64.b64decode(value).decode("utf-8")
            basic_id, _, basic_secret = decoded.partition(":")
            client_id, client_secret = unquote(basic_id), unquote(basic_secret)
        except Exception as exc:
            raise OAuthError("invalid_client", "malformed Basic credentials", status=401) from exc
    return client_id, client_secret


async def _authenticate(form: dict, authorization: str | None) -> OAuthClient:
    client_id, client_secret = _client_credentials(form, authorization)
    if not client_id:
        raise OAuthError("invalid_client", "client_id is required", status=401)
    client = await resolve_client(client_id)
    if client.client_secret_digest:
        if not client_secret or not hmac.compare_digest(
                credentials.digest(client_secret), client.client_secret_digest):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
    return client


async def _issue(s, conn: McpConnection, family_id: str) -> dict:
    from nexus.core.config import get_settings

    settings = get_settings()
    now = utcnow()
    access = credentials.new_secret(ACCESS_PREFIX)
    refresh = credentials.new_secret(REFRESH_PREFIX)
    access_ttl = max(60, int(settings.oauth_access_token_ttl_s))
    s.add(OAuthToken(token_digest=credentials.digest(access), connection_id=conn.id,
                     kind="access", family_id=family_id,
                     expires_at=now + timedelta(seconds=access_ttl)))
    s.add(OAuthToken(token_digest=credentials.digest(refresh), connection_id=conn.id,
                     kind="refresh", family_id=family_id,
                     expires_at=now + timedelta(days=max(1, int(settings.oauth_refresh_token_ttl_days)))))
    conn.last_used_at = now
    return {"access_token": access, "token_type": "Bearer", "expires_in": access_ttl,
            "refresh_token": refresh, "scope": scope_string(conn.scopes)}


async def _revoke_family(s, family_id: str) -> None:
    await s.execute(update(OAuthToken).where(OAuthToken.family_id == family_id,
                                             OAuthToken.revoked_at.is_(None))
                    .values(revoked_at=utcnow()))


async def revoke_connection_tokens(s, connection_id: str) -> None:
    await s.execute(update(OAuthToken).where(OAuthToken.connection_id == connection_id,
                                             OAuthToken.revoked_at.is_(None))
                    .values(revoked_at=utcnow()))


async def exchange(form: dict, authorization: str | None) -> dict:
    grant = form.get("grant_type")
    if grant == "authorization_code":
        return await _exchange_code(form, authorization)
    if grant == "refresh_token":
        return await _refresh(form, authorization)
    raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")


async def _exchange_code(form: dict, authorization: str | None) -> dict:
    client = await _authenticate(form, authorization)
    code = str(form.get("code") or "")
    now = utcnow()
    async with get_sessionmaker()() as s:
        row = await s.get(OAuthCode, credentials.digest(code)) if code else None
        if row is None or row.client_id != client.client_id:
            raise OAuthError("invalid_grant", "the authorization code is not valid")
        if row.used_at is not None:
            # A replayed code: whatever it issued is suspect, so it stops working.
            await revoke_connection_tokens(s, row.connection_id)
            await s.commit()
            raise OAuthError("invalid_grant", "the authorization code was already used")
        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "the authorization code has expired")
        if str(form.get("redirect_uri") or "") != row.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization")
        if not pkce_ok(str(form.get("code_verifier") or ""), row.code_challenge):
            raise OAuthError("invalid_grant", "the code_verifier does not match")
        resource = str(form.get("resource") or "")
        if resource and row.resource and resource.rstrip("/") != row.resource.rstrip("/"):
            raise OAuthError("invalid_target", "resource does not match the authorization")
        row.used_at = now
        conn = await s.get(McpConnection, row.connection_id)
        if conn is None or conn.revoked_at is not None:
            raise OAuthError("invalid_grant", "this connection was revoked")
        conn.scopes = list(row.scopes)
        tokens = await _issue(s, conn, uuid.uuid4().hex)
        client_row = await s.get(OAuthClient, client.id)
        if client_row is not None:
            client_row.last_used_at = now
        await s.commit()
        credentials.forget(conn.id)
        return tokens


async def _refresh(form: dict, authorization: str | None) -> dict:
    client = await _authenticate(form, authorization)
    token = str(form.get("refresh_token") or "")
    now = utcnow()
    async with get_sessionmaker()() as s:
        row = await s.get(OAuthToken, credentials.digest(token)) if token else None
        if row is None or row.kind != "refresh":
            raise OAuthError("invalid_grant", "the refresh token is not valid")
        conn = await s.get(McpConnection, row.connection_id)
        if conn is None or conn.client_id != client.client_id:
            raise OAuthError("invalid_grant", "the refresh token is not valid")
        if row.used_at is not None or row.revoked_at is not None:
            # Rotation means a refresh token works once. Seeing it twice means it was copied, so
            # every token in its family stops, including the one the legitimate app holds now.
            await _revoke_family(s, row.family_id)
            await s.commit()
            credentials.forget(conn.id)
            raise OAuthError("invalid_grant", "the refresh token was already used")
        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "the refresh token has expired")
        if conn.revoked_at is not None:
            raise OAuthError("invalid_grant", "this connection was revoked")
        row.used_at = now
        tokens = await _issue(s, conn, row.family_id)
        await s.commit()
        return tokens


async def revoke_token(token: str) -> None:
    """RFC 7009: revoke an access or refresh token. Unknown tokens are not an error."""
    if not token:
        return
    async with get_sessionmaker()() as s:
        row = await s.get(OAuthToken, credentials.digest(token))
        if row is None:
            return
        if row.kind == "refresh":
            await _revoke_family(s, row.family_id)
        else:
            row.revoked_at = utcnow()
        await s.commit()
        credentials.forget(row.connection_id)
