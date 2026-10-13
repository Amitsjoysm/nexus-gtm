# nexus/api/routers/mcp.py
"""The MCP endpoint and NEXUS's OAuth server, at the ROOT of the app rather than under ``/api``.

MCP clients and OAuth clients find these by convention: ``/.well-known/oauth-protected-resource``,
``/.well-known/oauth-authorization-server`` and the ``/mcp`` path users paste. Registered before the
SPA catch-all in ``main.py``.

``POST /mcp`` in order: Origin, bearer, the workspace's ``module.mcp`` gate, the per-connection
rate limit (tool calls only), then ``nexus.mcp.server``. A missing or unusable bearer is a 401
carrying ``WWW-Authenticate: Bearer resource_metadata=...``, which is how Claude and ChatGPT
discover that they should start the OAuth flow; a 200 with that header would be ignored.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from nexus.mcp import credentials, protocol, urls

logger = logging.getLogger("nexus.api.routers.mcp")

router = APIRouter(tags=["mcp"], include_in_schema=False)

ALL_OAUTH_SCOPES = " ".join(credentials.OAUTH_SCOPE[s] for s in credentials.SCOPES)


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # server-to-server clients send none
    from nexus.core.config import get_settings

    settings = get_settings()
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.hostname:
        return False
    normalised = f"{parsed.scheme}://{parsed.netloc}".lower()
    base = urls.base_url(request).lower()
    if base and normalised == base:
        return True
    allowed = {o.strip().rstrip("/").lower()
               for o in (settings.mcp_allowed_origins or "").split(",") if o.strip()}
    if normalised in allowed:
        return True
    # A local stack accepts browser tools on loopback (MCP Inspector runs on localhost).
    return settings.env in ("local", "test") and parsed.hostname in ("localhost", "127.0.0.1")


def _unauthorized(request: Request, reason: str) -> JSONResponse:
    from nexus.core import metrics

    metrics.record_mcp_request("unknown", "unauthorized")
    challenge = (f'Bearer resource_metadata="{urls.resource_metadata_url(request)}", '
                 f'scope="{ALL_OAUTH_SCOPES}"')
    if reason != "missing":
        challenge += ', error="invalid_token"'
    return JSONResponse(
        {"error": "invalid_token" if reason != "missing" else "unauthorized",
         "error_description": {
             "missing": "Sign in to NEXUS: this endpoint needs a bearer token.",
             "expired": "The token has expired.",
             "revoked": "This connection was revoked in NEXUS.",
             "suspended": "This NEXUS account is suspended.",
             "no_membership": "This account is no longer a member of the workspace.",
         }.get(reason, "The token is not valid.")},
        status_code=401, headers={"WWW-Authenticate": challenge},
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _rpc(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


async def _module_refusal(actor: credentials.Actor) -> str | None:
    """Why this workspace cannot use AI apps right now, or None. Same rules as every metered
    action (`preflight`): a platform switch always refuses; a plan without `module.mcp` refuses
    only when enforcement is on."""
    from nexus.billing.entitlements import preflight
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls

    async with get_sessionmaker()() as s:
        await apply_rls(s, actor.tenant_id)
        decision = await preflight(TenantSession(s, actor.tenant_id), "module.mcp")
    if decision.allowed:
        return None
    ent = decision.entitlement
    if ent is not None and ent.switch_state:
        return ent.switch_message or "AI apps are switched off for every workspace right now."
    return "The workspace's plan does not include AI apps. An admin can change it in NEXUS > Billing."


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    from nexus.core import metrics, ratelimit
    from nexus.core.config import get_settings
    from nexus.mcp import server

    if not _origin_allowed(request):
        metrics.record_mcp_request("unknown", "forbidden_origin")
        return JSONResponse({"error": "forbidden", "error_description": "Origin not allowed."},
                            status_code=403)
    bearer = _bearer(request)
    if bearer is None:
        return _unauthorized(request, "missing")
    try:
        actor = await credentials.resolve(bearer)
    except credentials.CredentialError as exc:
        return _unauthorized(request, exc.reason)

    body = await request.body()
    try:
        req = protocol.parse(body)
    except protocol.ProtocolError as exc:
        metrics.record_mcp_request("unknown", "error")
        return _rpc(protocol.error(None, exc.code, exc.message, exc.data), 400)

    try:
        stateless_or_not, version = protocol.dialect(
            req, request.headers.get("mcp-protocol-version"))
        stateless = stateless_or_not == "stateless"
        header_method = request.headers.get("mcp-method")
        if stateless and header_method and header_method != req.method:
            raise protocol.ProtocolError(
                protocol.HEADER_MISMATCH, "the Mcp-Method header does not match the request")

        if req.method in ("tools/list", "tools/call"):
            refusal = await _module_refusal(actor)
            if refusal:
                metrics.record_mcp_request(req.method, "unavailable")
                raise protocol.ProtocolError(protocol.UNAVAILABLE, refusal)
        if req.method == "tools/call":
            retry = await ratelimit.hit(
                f"mcp:{actor.connection_id}", window=60,
                limit=max(1, int(get_settings().mcp_rate_limit_per_minute)))
            if retry is not None:
                metrics.record_mcp_request(req.method, "limited")
                return JSONResponse(
                    protocol.error(req.id, protocol.UNAVAILABLE,
                                   f"Too many calls from this connection; try again in {retry} "
                                   "seconds."),
                    status_code=429, headers={"Retry-After": str(retry)})

        response = await server.handle(request.app, actor, req, stateless=stateless,
                                       version=version)
    except protocol.ProtocolError as exc:
        metrics.record_mcp_request(req.method, "error")
        return _rpc(protocol.error(req.id, exc.code, exc.message, exc.data))
    except Exception:
        logger.exception("MCP %s failed", req.method)
        metrics.record_mcp_request(req.method, "error")
        return _rpc(protocol.error(req.id, protocol.INTERNAL_ERROR, "Internal error."))

    await credentials.touch(actor.connection_id)
    metrics.record_mcp_request(req.method, "ok")
    if response is None:
        return Response(status_code=202)
    return _rpc(response)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_other_methods() -> Response:
    """No server-pushed stream and no session to end: POST is the whole transport."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.get("/.well-known/openai-apps-challenge")
async def openai_apps_challenge() -> Response:
    """The ChatGPT app directory's domain check: the token its submission form shows, as text."""
    from nexus.core.config import get_settings

    token = (get_settings().openai_apps_challenge_token or "").strip()
    if not token:
        return Response(status_code=404)
    return PlainTextResponse(token)


# ---- OAuth discovery and the authorization server --------------------------------------------

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    from nexus.mcp import oauth

    return JSONResponse(oauth.protected_resource_metadata(urls.base_url(request)))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    from nexus.mcp import oauth

    return JSONResponse(oauth.authorization_server_metadata(urls.base_url(request)))


def _oauth_error_page(message: str, status: int = 400) -> Response:
    """Shown in the user's browser when we cannot safely redirect back to the app."""
    import html

    from fastapi.responses import HTMLResponse

    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Cannot connect this app</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
        "padding:0 1rem;line-height:1.5;color:#1d1d1f}h1{font-size:1.25rem}</style></head><body>"
        "<h1>This app cannot connect to NEXUS</h1>"
        f"<p>{html.escape(message)}</p>"
        "<p>Start again from the app. If it keeps happening, tell the app's developer.</p>"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=status)


@router.post("/oauth/register")
async def oauth_register(request: Request) -> JSONResponse:
    """Dynamic Client Registration (RFC 7591), kept for clients that do not use Client ID
    Metadata Documents. Rate limited per IP like the sign-in endpoints."""
    from nexus.core import metrics
    from nexus.core.ratelimit import rate_limit
    from nexus.mcp import oauth

    await rate_limit("oauth_register")(request)
    try:
        body = await request.json()
    except Exception:
        body = None
    try:
        registered = await oauth.register_client(body)
    except oauth.OAuthError as exc:
        metrics.record_oauth_event("register", "refused")
        return JSONResponse(exc.body(), status_code=400, headers=_NO_STORE)
    metrics.record_oauth_event("register", "ok")
    return JSONResponse(registered, status_code=201, headers=_NO_STORE)


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request) -> Response:
    from fastapi.responses import RedirectResponse

    from nexus.core import metrics
    from nexus.mcp import oauth

    try:
        result = await oauth.start_authorization(dict(request.query_params),
                                                 urls.base_url(request))
    except oauth.OAuthError as exc:
        metrics.record_oauth_event("authorize", "refused")
        return _oauth_error_page(exc.description)
    if result.error_redirect:
        metrics.record_oauth_event("authorize", "error_redirect")
        return RedirectResponse(result.error_redirect, status_code=302)
    metrics.record_oauth_event("authorize", "ok")
    return RedirectResponse(result.consent_url, status_code=302)


@router.post("/oauth/token")
async def oauth_token(request: Request) -> JSONResponse:
    from nexus.core import metrics
    from nexus.mcp import oauth

    form = dict(await request.form())
    event = "refresh" if form.get("grant_type") == "refresh_token" else "token"
    try:
        tokens = await oauth.exchange(form, request.headers.get("authorization"))
    except oauth.OAuthError as exc:
        metrics.record_oauth_event(event, exc.error)
        headers = dict(_NO_STORE)
        if exc.status == 401:
            headers["WWW-Authenticate"] = 'Basic realm="nexus"'
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)
    metrics.record_oauth_event(event, "ok")
    return JSONResponse(tokens, headers=_NO_STORE)


@router.post("/oauth/revoke")
async def oauth_revoke(request: Request) -> Response:
    """RFC 7009: always 200, whether or not the token existed."""
    from nexus.core import metrics
    from nexus.mcp import oauth

    form = dict(await request.form())
    await oauth.revoke_token(str(form.get("token") or ""))
    metrics.record_oauth_event("revoke", "ok")
    return Response(status_code=200, headers=_NO_STORE)
