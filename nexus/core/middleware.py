"""Cross-cutting HTTP middleware: request IDs and structured access logging.

Each request gets a correlation ID (honoring an inbound ``X-Request-ID`` if present) that is bound
to a context var so any log line emitted during the request can include it. One structured access
log line is emitted per request with method, path, status, and duration. No external dependencies.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = logging.getLogger("nexus.access")

# Baseline security headers. Set-if-absent, so a fronting proxy (Caddy) that already sets them
# stays authoritative and we never emit a conflicting duplicate. HSTS is added separately and
# only outside local/test — forcing HTTPS on a plain-HTTP localhost would break dev.
_BASE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
# Appended below, once _CSP is defined.
_HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

# Content-Security-Policy. Today nothing can inject script — the SPA has zero
# `dangerouslySetInnerHTML`, so React escapes every text node, and an audit confirmed stored
# `<script>` payloads render inert. That safety rests entirely on a convention, and one
# `dangerouslySetInnerHTML` added in a hurry would silently undo it. CSP is the control that
# survives that mistake.
#
# `'unsafe-inline'` on style-src is required by the build: Vite emits inline styles and
# framer-motion writes them at runtime. It is NOT set on script-src, which is the directive that
# actually stops XSS — an injected `<script>` or `onerror=` handler is refused by the browser even
# if it reaches the DOM.
#
# `connect-src 'self'` keeps a compromised page from exfiltrating to an attacker's host.
# `frame-ancestors 'none'` duplicates X-Frame-Options for browsers that prefer CSP.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)
_BASE_SECURITY_HEADERS["Content-Security-Policy"] = _CSP


def get_request_id() -> str | None:
    return _request_id.get()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers to every response (set-if-absent).

    Defense-in-depth for any deployment that runs without the Caddy edge (e.g. the root
    docker-compose, a bare `uvicorn`, an internal probe). When Caddy fronts the app it already
    sets these, so the set-if-absent check leaves its values untouched — no duplication.
    """

    def __init__(self, app, *, enable_hsts: bool) -> None:
        super().__init__(app)
        self._enable_hsts = enable_hsts

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _BASE_SECURITY_HEADERS.items():
            if name not in response.headers:
                response.headers[name] = value
        if self._enable_hsts and _HSTS_HEADER[0] not in response.headers:
            response.headers[_HSTS_HEADER[0]] = _HSTS_HEADER[1]
        return response


class MaxBodySizeMiddleware:
    """Reject request bodies larger than ``max_bytes`` with 413, before they are buffered.

    Pure-ASGI so it can refuse an oversized upload without the app ever reading it. Guards both
    declared sizes (``Content-Length``) and streamed/chunked bodies (counted as they arrive). A
    cheap defense against a memory/CPU DoS from a giant payload. ``max_bytes <= 0`` disables it.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if self.max_bytes <= 0 or scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Fast path: an honest Content-Length over the cap is refused immediately.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        return await self._too_large(send)
                except ValueError:
                    pass
                break

        # Slow path: count streamed bytes; refuse if the running total exceeds the cap.
        seen = 0
        too_large = False

        async def counting_receive():
            nonlocal seen, too_large
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    too_large = True
            return message

        if too_large:  # pragma: no cover - defensive
            return await self._too_large(send)
        await self.app(scope, counting_receive, send)

    @staticmethod
    async def _too_large(send) -> None:
        body = b'{"detail":"Request body too large."}'
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """De-duplicate mutating POSTs carrying an ``Idempotency-Key`` header (H-4).

    First request with a key runs normally and its JSON response is remembered; a duplicate with
    the same key replays that response (``Idempotent-Replay: true``) instead of re-running the
    work. A duplicate arriving while the first is still in flight gets ``409``. Requests without
    the header, non-POST requests, and non-JSON/streaming responses (e.g. SSE) are passed straight
    through untouched — so this can never buffer or break a streaming endpoint.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        key_header = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not key_header:
            return await call_next(request)

        from nexus.core.config import get_settings
        from nexus.core.idempotency import StoredResponse, get_idempotency_store

        settings = get_settings()
        if not settings.idempotency_enabled:
            return await call_next(request)

        # Scope the key by path + caller (auth header) so distinct users/routes can't collide.
        auth = request.headers.get("Authorization", "")
        composite = hashlib.sha256(
            f"{key_header}|{request.url.path}|{auth}".encode()
        ).hexdigest()
        store = get_idempotency_store()
        ttl = settings.idempotency_ttl_s

        if not await store.claim(composite, ttl):
            cached = await store.get(composite)
            if cached is not None:
                resp = Response(
                    content=cached.body, status_code=cached.status_code,
                    media_type="application/json",
                )
                resp.headers["Idempotent-Replay"] = "true"
                return resp
            # Claimed but no stored response yet → the original is still running.
            return JSONResponse(
                {"detail": "A request with this Idempotency-Key is already in progress."},
                status_code=409,
            )

        # We own the claim: run the handler, then cache a cacheable JSON response.
        try:
            response = await call_next(request)
        except Exception:
            await store.release(composite)  # let the client retry a failed request
            raise

        content_type = response.headers.get("content-type", "")
        if response.status_code >= 500 or not content_type.startswith("application/json"):
            # Never cache server errors or non-JSON/streaming responses; leave them untouched.
            await store.release(composite)
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        await store.complete(composite, StoredResponse(response.status_code, body.decode()), ttl)
        headers = {
            k: v for k, v in response.headers.items() if k.lower() != "content-length"
        }
        rebuilt = Response(
            content=body, status_code=response.status_code, headers=headers,
            media_type="application/json",
        )
        rebuilt.headers["Idempotent-Replay"] = "false"
        return rebuilt


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = _request_id.set(rid)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            logger.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round(duration_ms, 1),
                },
            )
            _request_id.reset(token)
