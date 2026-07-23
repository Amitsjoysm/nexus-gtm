"""Idempotency for mutating POSTs (H-4): store semantics + middleware behavior.

Default-off; these tests enable the flag explicitly. The middleware is exercised on a tiny app so
we control the handler and can assert single-execution, replay, in-progress conflict, and that
streaming/non-JSON responses are passed through untouched.
"""
from __future__ import annotations

import httpx
import pytest_asyncio
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from nexus.core.config import get_settings
from nexus.core.idempotency import MemoryIdempotencyStore, StoredResponse, set_idempotency_store
from nexus.core.middleware import IdempotencyMiddleware


# ---- store ----

async def test_memory_store_claim_get_complete_release():
    store = MemoryIdempotencyStore()
    assert await store.claim("k", 60) is True          # first claim wins
    assert await store.claim("k", 60) is False         # second sees it exists
    assert await store.get("k") is None                # in-progress → no stored response yet
    await store.complete("k", StoredResponse(201, '{"ok":true}'), 60)
    got = await store.get("k")
    assert got is not None and got.status_code == 201 and got.body == '{"ok":true}'


async def test_memory_store_release_allows_reclaim():
    store = MemoryIdempotencyStore()
    assert await store.claim("k", 60) is True
    await store.release("k")                            # unfinished claim dropped
    assert await store.claim("k", 60) is True           # can be reclaimed


async def test_memory_store_release_never_clobbers_completed():
    store = MemoryIdempotencyStore()
    await store.claim("k", 60)
    await store.complete("k", StoredResponse(200, "{}"), 60)
    await store.release("k")                            # must be a no-op on a finished key
    assert await store.get("k") is not None


# ---- middleware ----

@pytest_asyncio.fixture(autouse=True)
def _enable_and_isolate(monkeypatch):
    monkeypatch.setattr(get_settings(), "idempotency_enabled", True)
    set_idempotency_store(MemoryIdempotencyStore())
    yield
    set_idempotency_store(None)


def _app_with_counter():
    calls = {"n": 0}

    async def create(request):
        calls["n"] += 1
        return JSONResponse({"id": calls["n"]}, status_code=201)

    async def stream(request):
        async def gen():
            yield b"data: 1\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/create", create, methods=["POST"]),
                            Route("/stream", stream, methods=["POST"])])
    app.add_middleware(IdempotencyMiddleware)
    return app, calls


@pytest_asyncio.fixture
async def client_and_calls():
    app, calls = _app_with_counter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, calls


async def test_duplicate_key_replays_and_runs_once(client_and_calls):
    c, calls = client_and_calls
    h = {"Idempotency-Key": "abc"}
    r1 = await c.post("/create", headers=h)
    r2 = await c.post("/create", headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json() == {"id": 1}     # same response
    assert calls["n"] == 1                          # handler ran exactly once
    assert r1.headers["Idempotent-Replay"] == "false"
    assert r2.headers["Idempotent-Replay"] == "true"


async def test_different_keys_run_independently(client_and_calls):
    c, calls = client_and_calls
    await c.post("/create", headers={"Idempotency-Key": "k1"})
    await c.post("/create", headers={"Idempotency-Key": "k2"})
    assert calls["n"] == 2


async def test_no_key_header_is_passthrough(client_and_calls):
    c, calls = client_and_calls
    await c.post("/create")
    await c.post("/create")
    assert calls["n"] == 2                           # no dedup without the header
    r = await c.post("/create")
    assert "Idempotent-Replay" not in r.headers


async def test_in_progress_duplicate_gets_409():
    # Pre-claim the key (simulating an original still running) so the request sees an in-progress claim.
    store = MemoryIdempotencyStore()
    set_idempotency_store(store)
    key_path_auth = "dup|/create|"
    import hashlib
    composite = hashlib.sha256(key_path_auth.encode()).hexdigest()
    await store.claim(composite, 60)

    app, _ = _app_with_counter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/create", headers={"Idempotency-Key": "dup"})
    assert r.status_code == 409


async def test_streaming_response_is_passed_through(client_and_calls):
    c, _ = client_and_calls
    # An SSE response must not be buffered/cached — it streams through with its content-type intact.
    r = await c.post("/stream", headers={"Idempotency-Key": "s1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "Idempotent-Replay" not in r.headers
