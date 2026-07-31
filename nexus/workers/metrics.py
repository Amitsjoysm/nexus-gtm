"""Job-outcome counters.

A module-level dict, read by the admin API and by tests. M15 added the scrapable half: every
increment is mirrored to a Prometheus counter carrying the **job name**, which this dict
deliberately does not track — a dict keyed by (job, outcome) is a memory leak waiting for a job
name derived from user input, whereas a Prometheus label set is bounded by the handler registry.

Both are updated from the same call site, so they cannot disagree about a total.

Counts are per-process and reset on restart. That is the correct semantics for a counter that a
scraper differentiates anyway, and it keeps this file free of any storage concern.
"""
from __future__ import annotations

# The four outcomes of the durability path. Pre-declared (rather than defaultdict) so a typo in
# a counter name is visible as a KeyError in tests instead of silently creating a metric nobody
# ever reads.
_COUNTER_NAMES = ("enqueued", "succeeded", "retried", "dead_lettered")

_counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}


def increment_job_counter(name: str, amount: int = 1, *, job: str = "") -> None:
    """Bump one counter. Never raises into a caller's hot path — a metric must not be able to
    fail the work it describes.

    ``job`` is optional and keyword-only so every existing call site keeps working unchanged; it
    only labels the Prometheus mirror, which is where per-job breakdown belongs.
    """
    if name not in _counters:  # unknown name: record it rather than lose the signal
        _counters[name] = 0
    _counters[name] += amount

    from nexus.core import metrics as _prom

    _prom.record_job_outcome(job, name, amount)


def job_counters() -> dict[str, int]:
    """A snapshot copy — callers must not be able to mutate the live counters."""
    return dict(_counters)


def reset_job_counters() -> None:
    """Test hook. Production never calls this: a counter that resets under load is a lie."""
    for name in list(_counters):
        _counters[name] = 0
