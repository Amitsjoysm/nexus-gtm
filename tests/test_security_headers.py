"""App-layer security headers (C-4) and CORS narrowing (M-3).

These are defense-in-depth for deployments that run without the Caddy edge. The headers are
set-if-absent so Caddy stays authoritative when it fronts the app.
"""
from __future__ import annotations

import httpx
import pytest_asyncio
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from nexus.core.middleware import SecurityHeadersMiddleware
from nexus.main import create_app


@pytest_asyncio.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_baseline_security_headers_present(client):
    r = await client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_hsts_absent_outside_prod(client):
    # Tests run with NEXUS_ENV=test; HSTS must NOT be emitted (it would pin HTTPS on localhost).
    r = await client.get("/health")
    assert "Strict-Transport-Security" not in r.headers


async def test_headers_on_error_responses_too(client):
    # A 404 still carries the baseline headers (the middleware wraps every response).
    r = await client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_set_if_absent_does_not_override_upstream():
    """When a fronting proxy already set a header, the middleware must leave it untouched."""

    async def _endpoint(request):
        # Simulate Caddy having set a stricter/branded value upstream.
        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    inner = Starlette(routes=[Route("/x", _endpoint)])
    inner.add_middleware(SecurityHeadersMiddleware, enable_hsts=True)
    transport = httpx.ASGITransport(app=inner)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/x")
    # Upstream value preserved (not overwritten to DENY); the other headers still added.
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


async def test_hsts_emitted_when_enabled():
    async def _endpoint(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/x", _endpoint)])
    inner.add_middleware(SecurityHeadersMiddleware, enable_hsts=True)
    transport = httpx.ASGITransport(app=inner)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/x")
    assert "includeSubDomains" in r.headers["Strict-Transport-Security"]


async def test_oversized_body_rejected_with_413(client):
    """A body over the configured cap is refused with 413 (DoS guard). Default cap is 10 MB."""
    big = "x" * (10_000_001)  # just over the 10 MB default
    r = await client.post(
        "/api/auth/login",
        content='{"email":"a@b.c","password":"' + big + '"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.text.lower()


async def test_normal_body_passes_the_size_guard(client):
    """A small body is unaffected — the guard only trips on oversized payloads. A normal request
    reaches the handler (any non-413 status proves the size guard did not block it)."""
    r = await client.post(
        "/api/auth/login",
        json={"email": "nobody@nowhere.test", "password": "whatever123"},
    )
    assert r.status_code != 413  # the request was processed, not refused for size
