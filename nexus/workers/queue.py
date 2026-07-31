"""Task queue abstraction.

Two backends behind one interface:
  * :class:`InMemoryTaskQueue` — an ``asyncio.Queue`` for local/dev/test (zero external deps).
  * :class:`RedisTaskQueue` — a Redis list for production (multiple workers, durability).

A job is a small JSON-serializable envelope: ``{"name": str, "payload": dict}`` plus the
retry bookkeeping added in M11.
"""
from __future__ import annotations

import abc
import asyncio
import json
from dataclasses import dataclass

from nexus.core.config import get_settings
from nexus.workers.metrics import increment_job_counter

_QUEUE_KEY = "nexus:jobs"

# How many times a job is attempted in total before it is parked in ``dead_letter_jobs``.
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(slots=True)
class Job:
    """One unit of queued work.

    ``attempts``/``max_attempts`` carry the retry state *in the envelope* rather than in worker
    memory, because the worker that retries a job is very often not the worker that first ran
    it (Valkey fans jobs out across the fleet, and a container can be replaced mid-backoff).

    Both fields default, and ``from_json`` reads them with defaults, so a job serialized by the
    PREVIOUS release — ``{"name": ..., "payload": ...}``, still sitting in Valkey during a
    rolling deploy — deserializes cleanly instead of blowing up on its first dequeue. That
    backward compatibility is the difference between a rolling upgrade and losing the backlog.
    """

    name: str
    payload: dict
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def to_json(self) -> str:
        # ``name`` and ``payload`` stay top-level, so an OLD worker still reading only those two
        # keys during a rolling deploy processes a new-format job correctly (it just loses the
        # retry counters, degrading to today's behaviour rather than erroring).
        return json.dumps(
            {
                "name": self.name,
                "payload": self.payload,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "Job":
        data = json.loads(raw)
        return cls(
            name=data["name"],
            payload=data.get("payload", {}),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        )


class TaskQueue(abc.ABC):
    @abc.abstractmethod
    async def enqueue(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def dequeue(self, *, timeout: float | None = None) -> Job | None: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None

    async def depth(self) -> int | None:
        """Jobs waiting to be picked up, or None if this backend cannot say.

        Concrete rather than abstract so adding it does not break any queue implementation that
        predates it. None means "unknown", which a metric must render as absent — reporting 0 for
        an unmeasurable queue would look exactly like a healthy empty one.
        """
        return None


class InMemoryTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue()

    async def enqueue(self, job: Job) -> None:
        increment_job_counter("enqueued", job=job.name)
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

    async def depth(self) -> int | None:
        return self._q.qsize()


class RedisTaskQueue(TaskQueue):
    def __init__(self, redis_url: str, key: str = _QUEUE_KEY) -> None:
        import redis.asyncio as redis  # imported lazily; optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._key = key

    async def enqueue(self, job: Job) -> None:
        increment_job_counter("enqueued", job=job.name)
        await self._redis.rpush(self._key, job.to_json())

    async def dequeue(self, *, timeout: float | None = None) -> Job | None:
        # BLPOP timeout of 0 blocks forever; translate None → 0.
        res = await self._redis.blpop(self._key, timeout=int(timeout or 0))
        if res is None:
            return None
        _, raw = res
        return Job.from_json(raw)

    async def depth(self) -> int | None:
        """LLEN, not a tracked counter: the list is the truth, and a counter would drift the
        moment a worker died holding a job."""
        try:
            return int(await self._redis.llen(self._key))
        except Exception:
            return None

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
