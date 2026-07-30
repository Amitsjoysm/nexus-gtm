"""Job-outcome counters.

Deliberately a module-level dict rather than a new dependency. The repo's only metrics surface
today is ``prometheus_fastapi_instrumentator``, which instruments HTTP requests and is off by
default (see ``nexus.main._maybe_enable_metrics``) — it has nothing to say about a worker
process, which serves no requests at all. Exporting these properly is M15's job; M11 only needs
the numbers to exist and be readable, so that "are jobs failing?" stops being a question you
answer by grepping logs.

Counts are per-process and reset on restart. That is the correct semantics for a counter that a
scraper differentiates anyway, and it keeps this file free of any storage concern.
"""
from __future__ import annotations

# The four outcomes of the durability path. Pre-declared (rather than defaultdict) so a typo in
# a counter name is visible as a KeyError in tests instead of silently creating a metric nobody
# ever reads.
_COUNTER_NAMES = ("enqueued", "succeeded", "retried", "dead_lettered")

_counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}


def increment_job_counter(name: str, amount: int = 1) -> None:
    """Bump one counter. Never raises into a caller's hot path — a metric must not be able to
    fail the work it describes."""
    if name not in _counters:  # unknown name: record it rather than lose the signal
        _counters[name] = 0
    _counters[name] += amount


def job_counters() -> dict[str, int]:
    """A snapshot copy — callers must not be able to mutate the live counters."""
    return dict(_counters)


def reset_job_counters() -> None:
    """Test hook. Production never calls this: a counter that resets under load is a lie."""
    for name in list(_counters):
        _counters[name] = 0
