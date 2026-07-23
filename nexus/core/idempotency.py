"""Idempotency store for de-duplicating mutating POSTs (H-4).

A client that sends the same ``Idempotency-Key`` twice (rage-click, client retry, proxy retry)
must not run the work twice. The store records, per key, either an in-progress claim or the
finished response, so a duplicate replays the first result.

Two backends, mirroring the task queue:
  * :class:`MemoryIdempotencyStore` — an in-process dict with TTL, for single-node dev/test.
  * :class:`RedisIdempotencyStore` — shared across workers, for production (the case that matters:
    two workers must not both execute the same keyed request).

Selected by ``NEXUS_QUEUE_BACKEND`` so idempotency and the queue share one infra decision.
"""
from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass

from nexus.core.config import get_settings


@dataclass(slots=True)
class StoredResponse:
    status_code: int
    body: str  # the response body, as text (JSON responses only — see the middleware guard)


class IdempotencyStore(abc.ABC):
    @abc.abstractmethod
    async def claim(self, key: str, ttl_s: int) -> bool:
        """Atomically claim a key. Returns True if this caller won the claim (first time seen),
        False if the key already exists (a concurrent or prior request owns it)."""

    @abc.abstractmethod
    async def get(self, key: str) -> StoredResponse | None:
        """Return the finished response for a key, or None if absent / still in-progress."""

    @abc.abstractmethod
    async def complete(self, key: str, response: StoredResponse, ttl_s: int) -> None:
        """Record the finished response so later duplicates replay it."""

    @abc.abstractmethod
    async def release(self, key: str) -> None:
        """Drop an unfinished claim (e.g. the handler errored) so the client can retry."""


_IN_PROGRESS = "\x00in-progress"


class MemoryIdempotencyStore(IdempotencyStore):
    """Single-process store. Correct for one worker; a fleet needs the Redis backend."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}  # key -> (payload, expires_at)

    def _purge_if_expired(self, key: str) -> None:
        item = self._data.get(key)
        if item is not None and item[1] <= time.monotonic():
            self._data.pop(key, None)

    async def claim(self, key: str, ttl_s: int) -> bool:
        self._purge_if_expired(key)
        if key in self._data:
            return False
        self._data[key] = (_IN_PROGRESS, time.monotonic() + ttl_s)
        return True

    async def get(self, key: str) -> StoredResponse | None:
        self._purge_if_expired(key)
        item = self._data.get(key)
        if item is None or item[0] == _IN_PROGRESS:
            return None
        payload = json.loads(item[0])
        return StoredResponse(status_code=payload["status"], body=payload["body"])

    async def complete(self, key: str, response: StoredResponse, ttl_s: int) -> None:
        payload = json.dumps({"status": response.status_code, "body": response.body})
        self._data[key] = (payload, time.monotonic() + ttl_s)

    async def release(self, key: str) -> None:
        item = self._data.get(key)
        if item is not None and item[0] == _IN_PROGRESS:
            self._data.pop(key, None)


class RedisIdempotencyStore(IdempotencyStore):
    """Cross-worker store. ``SET key <sentinel> NX EX`` is the atomic claim."""

    def __init__(self, redis_url: str, prefix: str = "nexus:idem:") -> None:
        import redis.asyncio as redis  # imported lazily; optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return self._prefix + key

    async def claim(self, key: str, ttl_s: int) -> bool:
        # NX = only set if absent → exactly one caller wins the claim.
        got = await self._redis.set(self._k(key), _IN_PROGRESS, nx=True, ex=ttl_s)
        return bool(got)

    async def get(self, key: str) -> StoredResponse | None:
        raw = await self._redis.get(self._k(key))
        if raw is None or raw == _IN_PROGRESS:
            return None
        payload = json.loads(raw)
        return StoredResponse(status_code=payload["status"], body=payload["body"])

    async def complete(self, key: str, response: StoredResponse, ttl_s: int) -> None:
        payload = json.dumps({"status": response.status_code, "body": response.body})
        await self._redis.set(self._k(key), payload, ex=ttl_s)

    async def release(self, key: str) -> None:
        # Only delete if it is still an unfinished claim (never clobber a stored response).
        if await self._redis.get(self._k(key)) == _IN_PROGRESS:
            await self._redis.delete(self._k(key))


_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        settings = get_settings()
        if settings.queue_backend == "redis":
            _store = RedisIdempotencyStore(settings.redis_url)
        else:
            _store = MemoryIdempotencyStore()
    return _store


def set_idempotency_store(store: IdempotencyStore | None) -> None:
    """Test/runtime override."""
    global _store
    _store = store
