# nexus/core/metrics.py
"""Domain metrics: what the business is doing, not just what HTTP is doing.

``prometheus-fastapi-instrumentator`` covers request rate, latency and status. That answers "is
the app up?" and nothing else. It cannot tell you that enforcement would have blocked 4,000
requests if you flipped it on, that a webhook signature is failing, or that the dead-letter queue
is growing — the three questions that actually decide whether billing can be enforced.

Three rules this module keeps:

* **A metric can never fail the work it describes.** Every entry point swallows everything. If
  ``prometheus_client`` is absent (it ships with the optional ``metrics`` extra), every function
  is a no-op. Nothing here is on a critical path.
* **Counters only, in the API process.** The app runs uvicorn with 2 workers, so there are two
  registries; ``PROMETHEUS_MULTIPROC_DIR`` makes counters and histograms aggregate correctly
  across them, but gauges need a declared mode and custom collectors are not read at all. So
  state lives in the worker (see ``nexus/workers/state_metrics.py``), which is single-process.
* **Names are declared, not derived.** A typo in a metric name is otherwise a metric nobody ever
  reads, and it looks exactly like "nothing happened".
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.core.metrics")

# Populated on first use; keyed by metric name so each collector is created exactly once per
# process (creating one twice raises "Duplicated timeseries in CollectorRegistry").
_METRICS: dict[str, object] = {}
_UNAVAILABLE = False


def _client():
    """The ``prometheus_client`` module, or None when the metrics extra is not installed."""
    global _UNAVAILABLE
    if _UNAVAILABLE:
        return None
    try:
        import prometheus_client

        return prometheus_client
    except Exception:
        _UNAVAILABLE = True
        return None


def _counter(name: str, doc: str, labels: tuple[str, ...]):
    return _collector("counter", name, doc, labels)


def _histogram(name: str, doc: str, labels: tuple[str, ...], buckets: tuple[float, ...]):
    return _collector("histogram", name, doc, labels, buckets=buckets)


def _collector(kind: str, name: str, doc: str, labels: tuple[str, ...], **kw):
    existing = _METRICS.get(name)
    if existing is not None:
        return existing
    client = _client()
    if client is None:
        return None
    try:
        cls = client.Counter if kind == "counter" else client.Histogram
        metric = cls(name, doc, labelnames=labels, **kw)
    except Exception:
        # Almost always a duplicate registration from a re-imported module in tests. Fall back to
        # whatever is already registered rather than losing the metric or raising.
        metric = _find_registered(name)
        if metric is None:
            logger.debug("metric %s unavailable", name, exc_info=True)
            return None
    _METRICS[name] = metric
    return metric


def _find_registered(name: str):
    client = _client()
    if client is None:
        return None
    try:
        return client.REGISTRY._names_to_collectors.get(name)  # noqa: SLF001
    except Exception:
        return None


def _observe(metric, labels: dict[str, str], *, inc: float | None = None,
             observe: float | None = None) -> None:
    """Apply one sample. Swallows everything — see the module docstring."""
    if metric is None:
        return
    try:
        child = metric.labels(**labels) if labels else metric
        if inc is not None:
            child.inc(inc)
        else:
            child.observe(observe)
    except Exception:
        logger.debug("metric update failed", exc_info=True)


# ---- billing decisions ----------------------------------------------------------------------

# The reason this milestone exists. `would_block` is the number that decides whether enforcement
# can be turned on: in shadow mode the engine computes it on every call and then throws it away,
# so today "what happens if we flip the switch?" is unanswerable except by flipping it.
#
# `outcome` and `reason` are separate labels on purpose. Folding them into one would force a
# choice between losing WHY a request was refused and losing WHETHER it actually was — a
# shadow-mode throttle and an enforced 429 are the same reason and opposite outcomes.
BILLING_DECISION_OUTCOMES = ("allowed", "would_block", "blocked", "error")


def record_billing_decision(capability_id: str, outcome: str, reason: str = "") -> None:
    _observe(
        _counter(
            "nexus_billing_decisions_total",
            "Entitlement decisions by capability, outcome and reason. `would_block` is a block "
            "the engine computed while in shadow mode and let through anyway.",
            ("capability", "outcome", "reason"),
        ),
        {"capability": capability_id, "outcome": outcome, "reason": reason or "none"},
        inc=1,
    )


def observe_entitlement_resolve(seconds: float) -> None:
    """Latency of one entitlement resolution — it sits in front of metered work, so if it is slow
    everything metered is slow."""
    _observe(
        _histogram(
            "nexus_entitlement_resolve_seconds",
            "Time to resolve a tenant's entitlement for one capability.",
            (),
            (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
        ),
        {},
        observe=seconds,
    )


def record_credit_burn(capability_id: str, credits: float) -> None:
    """Credits actually consumed. Rate of this against the plan's included credits is what says
    a tenant is about to hit an overage — before the invoice tells them."""
    if credits <= 0:
        return
    _observe(
        _counter(
            "nexus_billing_credits_burned_total",
            "Credits burned, by capability.",
            ("capability",),
        ),
        {"capability": capability_id},
        inc=float(credits),
    )


# ---- webhooks -------------------------------------------------------------------------------

# A rejected webhook writes NO row by design — the dedupe table only records events that verified.
# So a wrong signing secret is invisible in the database: subscriptions simply stop updating, and
# the first report is a customer asking why they were charged for a plan they cancelled.
WEBHOOK_OUTCOMES = ("processed", "ignored", "duplicate", "bad_signature", "stale", "error")


def record_webhook_event(provider: str, outcome: str) -> None:
    _observe(
        _counter(
            "nexus_webhook_events_total",
            "Provider webhook deliveries by outcome. `bad_signature` and `stale` are rejections "
            "that persist nothing, so this counter is the only trace they leave.",
            ("provider", "outcome"),
        ),
        {"provider": provider, "outcome": outcome},
        inc=1,
    )


# ---- jobs -----------------------------------------------------------------------------------

# ---- signal sources -------------------------------------------------------------------------

# `empty` is a distinct outcome from `ok` on purpose: a source that runs cleanly and finds nothing
# every single time is a broken source, and folding the two together hides precisely that. Before
# these existed, a source failing for a week and a quiet market produced identical evidence.
SOURCE_RUN_OUTCOMES = ("ok", "empty", "timeout", "error")


def record_source_run(source: str, outcome: str, items_found: int = 0) -> None:
    _observe(
        _counter(
            "nexus_signal_source_runs_total",
            "Signal source runs by source and outcome. `empty` means the source ran cleanly and "
            "returned nothing, which is not the same as healthy.",
            ("source", "outcome"),
        ),
        {"source": source or "unknown", "outcome": outcome},
        inc=1,
    )
    if items_found > 0:
        _observe(
            _counter(
                "nexus_signal_items_found_total",
                "Raw items returned by signal sources, before dedupe.",
                ("source",),
            ),
            {"source": source or "unknown"},
            inc=items_found,
        )


def record_job_outcome(job_name: str, outcome: str, amount: int = 1) -> None:
    """Mirrors ``nexus/workers/metrics.py``, which stays as the in-process dict the admin API and
    tests read. This is the scrapable half; the two are updated from the same call site."""
    _observe(
        _counter(
            "nexus_jobs_total",
            "Job outcomes by job name.",
            ("job", "outcome"),
        ),
        {"job": job_name or "unknown", "outcome": outcome},
        inc=amount,
    )


# ---- test/debug helper ----------------------------------------------------------------------

def metric_value(name: str, labels: dict[str, str] | None = None) -> float | None:
    """Current value of one sample, or None if the metric or label set has no value yet.

    For tests. Counters cannot be reset, so assertions compare a delta around the call under
    test rather than an absolute.
    """
    client = _client()
    if client is None:
        return None
    try:
        return client.REGISTRY.get_sample_value(name, labels or {})
    except Exception:
        return None
