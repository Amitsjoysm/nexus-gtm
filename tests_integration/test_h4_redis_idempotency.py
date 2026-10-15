"""H-4 on real Redis: the idempotency store's `SET NX` claim is atomic across concurrent callers
(the multi-worker case the in-process store cannot cover), and the replay cycle round-trips."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus.core.idempotency import RedisIdempotencyStore, StoredResponse
from tests_integration.conftest import REDIS_URL, requires_redis

pytestmark = [pytest.mark.asyncio, requires_redis]


def _store() -> RedisIdempotencyStore:
    # Unique prefix per test run so leftover keys never cross-contaminate.
    return RedisIdempotencyStore(REDIS_URL, prefix=f"nexus:idem:test:{uuid.uuid4().hex}:")


async def test_claim_is_atomic_across_concurrent_callers():
    """Exactly one of N concurrent claims on the same key wins — the guarantee that makes two
    workers processing the same Idempotency-Key run the work only once."""
    store = _store()
    key = "same-key"
    results = await asyncio.gather(*[store.claim(key, 60) for _ in range(20)])
    assert sum(1 for r in results if r) == 1, results  # exactly one winner


async def test_replay_cycle_round_trips():
    store = _store()
    key = "k"
    assert await store.claim(key, 60) is True
    assert await store.get(key) is None  # claimed, not yet completed → no replay
    await store.complete(key, StoredResponse(201, '{"id": 7}'), 60)
    got = await store.get(key)
    assert got is not None and got.status_code == 201 and got.body == '{"id": 7}'
    # A later claim on a completed key does not win (the response is remembered, replay applies).
    assert await store.claim(key, 60) is False


async def test_release_frees_an_unfinished_claim_only():
    store = _store()
    key = "k"
    await store.claim(key, 60)
    await store.release(key)                 # unfinished → dropped
    assert await store.claim(key, 60) is True

    await store.complete(key, StoredResponse(200, "{}"), 60)
    await store.release(key)                 # finished → must NOT be dropped
    assert await store.get(key) is not None


async def test_factory_selects_redis_when_queue_backend_is_redis(monkeypatch):
    """get_idempotency_store() must return the Redis-backed store in production config."""
    from nexus.core.config import get_settings
    from nexus.core import idempotency as idem

    monkeypatch.setattr(get_settings(), "queue_backend", "redis")
    monkeypatch.setattr(get_settings(), "redis_url", REDIS_URL)
    idem.set_idempotency_store(None)  # clear the cached singleton
    try:
        store = idem.get_idempotency_store()
        assert isinstance(store, RedisIdempotencyStore)
    finally:
        idem.set_idempotency_store(None)


async def test_the_job_queue_claim_is_one_holder_until_released_or_expired():
    """The heartbeat's driver guard and the once-a-day reconciliation on real Redis (2026-10-14):
    `SET NX EX` is held by one caller across concurrent workers, and release frees it at once."""
    from nexus.workers.queue import RedisTaskQueue

    queue = RedisTaskQueue(REDIS_URL, key=f"nexus:jobs:test:{uuid.uuid4().hex}")
    key = f"nexus:driver:test:{uuid.uuid4().hex}"
    try:
        results = await asyncio.gather(*[queue.claim(key, 60) for _ in range(20)])
        assert sum(1 for r in results if r) == 1, results
        await queue.release(key)
        assert await queue.claim(key, 1) is True
        await asyncio.sleep(1.5)                     # an expired claim can be taken again
        assert await queue.claim(key, 60) is True
    finally:
        await queue.release(key)
        await queue.aclose()
