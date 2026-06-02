"""Lightweight in-process event bus.

Decouples subsystems: ingestion emits ``signal.created``, the plays engine and inbox subscribe.
In production this is the seam where you'd swap an outbox/broker; the interface stays the same.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("nexus.events")


@dataclass(slots=True)
class Event:
    name: str
    tenant_id: str
    payload: dict = field(default_factory=dict)
    # Orchestration correlation (optional, additive). These let subscribers trace an
    # event back to the run/step that produced it and the event that caused it — the
    # envelope a real broker (Redis Streams/NATS) needs, without changing the API.
    run_id: str | None = None
    step_id: str | None = None
    causation_id: str | None = None


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, name: str, handler: Handler) -> None:
        self._handlers[name].append(handler)

    async def publish(self, event: Event) -> None:
        handlers = self._handlers.get(event.name, [])
        if not handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in handlers), return_exceptions=True
        )
        for h, r in zip(handlers, results):
            if isinstance(r, Exception):
                logger.exception("handler %s failed for %s: %r", h, event.name, r)


_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus
