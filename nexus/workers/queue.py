"""Task queue abstraction.

Two backends behind one interface:
  * :class:`InMemoryTaskQueue` — an ``asyncio.Queue`` for local/dev/test (zero external deps).
  * :class:`RedisTaskQueue` — a Redis list for production (multiple workers, durability).

A job is a small JSON-serializable envelope: ``{"name": str, "payload": dict}``.
"""
from __future__ import annotations

import abc
import asyncio
import json
from dataclasses import dataclass

from nexus.core.config import get_settings

_QUEUE_KEY = "nexus:jobs"


@dataclass(slots=True)
class Job:
    name: str
    payload: dict

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "payload": self.payload})

    @classmethod
    def from_json(cls, raw: str) -> "Job":
        data = json.loads(raw)
        return cls(name=data["name"], payload=data.get("payload", {}))


class TaskQueue(abc.ABC):
    @abc.abstractmethod
    async def enqueue(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def dequeue(self, *, timeout: float | None = None) -> Job | None: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue()

    async def enqueue(self, job: Job) -> None:
        await self._q.put(job)

    async def dequeue(self, *, timeout: float | None = None) -> Job | None:
        if timeout is None:
            return await self._q.get()
        if timeout == 0:
            # Non-blocking poll: return immediately without a wait_for (which always
            # times out at timeout=0 in Python 3.10+ even when the queue is non-empty).
            try:
                return self._q.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(self._q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class RedisTaskQueue(TaskQueue):
    def __init__(self, redis_url: str, key: str = _QUEUE_KEY) -> None:
        import redis.asyncio as redis  # imported lazily; optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._key = key

    async def enqueue(self, job: Job) -> None:
        await self._redis.rpush(self._key, job.to_json())

    async def dequeue(self, *, timeout: float | None = None) -> Job | None:
        # BLPOP timeout of 0 blocks forever; translate None → 0.
        res = await self._redis.blpop(self._key, timeout=int(timeout or 0))
        if res is None:
            return None
        _, raw = res
        return Job.from_json(raw)

    async def aclose(self) -> None:
        await self._redis.aclose()


_queue: TaskQueue | None = None


def get_task_queue() -> TaskQueue:
    global _queue
    if _queue is None:
        settings = get_settings()
        if settings.queue_backend == "redis":
            _queue = RedisTaskQueue(settings.redis_url)
        else:
            _queue = InMemoryTaskQueue()
    return _queue


def set_task_queue(queue: TaskQueue | None) -> None:
    global _queue
    _queue = queue
