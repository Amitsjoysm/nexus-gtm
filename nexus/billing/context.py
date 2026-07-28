# nexus/billing/context.py
"""A context-local accumulator for the true cost of servicing one billable action.

The LLM/search layers know what a call COST but not what it was FOR; the endpoint knows what it
was FOR but not what it cost. A ContextVar joins them without threading a parameter through every
call site, and — unlike a module global — keeps two concurrent requests' costs separate.

``report_cost`` is deliberately a no-op when no scope is active: infrastructure must never fail
because nothing happens to be metering it (a background job, a test, a script).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class CostAccumulator:
    """Real spend incurred inside one metered action."""

    usd: float = 0.0
    tokens: int = 0
    calls: int = 0
    detail: list[dict] = field(default_factory=list)

    def add(self, *, usd: float, tokens: int = 0, source: str = "") -> None:
        self.usd += float(usd or 0)
        self.tokens += int(tokens or 0)
        self.calls += 1
        if source:
            self.detail.append({"source": source, "usd": float(usd or 0), "tokens": tokens})


_cost: ContextVar[CostAccumulator | None] = ContextVar("nexus_billing_cost", default=None)


@contextmanager
def cost_scope() -> Iterator[CostAccumulator]:
    """Collect costs reported anywhere inside this block (including nested awaits)."""
    acc = CostAccumulator()
    token = _cost.set(acc)
    try:
        yield acc
    finally:
        _cost.reset(token)


def report_cost(*, usd: float, tokens: int = 0, source: str = "") -> None:
    """Report real spend. Silently ignored when no scope is active — by design."""
    acc = _cost.get()
    if acc is not None:
        acc.add(usd=usd, tokens=tokens, source=source)
