"""Infra hardening: readiness probe and request-ID propagation."""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_readiness_checks_db(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "db": "up"}


async def test_request_id_header_is_returned(client):
    r = await client.get("/health")
    assert r.headers.get("X-Request-ID")


async def test_inbound_request_id_is_echoed(client):
    r = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers.get("X-Request-ID") == "trace-123"


async def test_signals_pagination_limits(client):
    h = auth(await signup(client))
    r = await client.get("/api/signals?limit=1&offset=0", headers=h)
    assert r.status_code == 200
    assert len(r.json()) <= 1
