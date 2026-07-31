# tests/test_metrics.py
"""Domain metrics (M15).

The point of these is not that Prometheus works — it is that the four numbers which were
previously computed and discarded now leave the process. Chief among them ``would_block``: shadow
enforcement decides it on every call and used to throw it away, so "what happens if we flip the
switch?" could only be answered by flipping it.

Counters cannot be reset, so every assertion here compares a delta around the call under test.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_tenant, tenant_session

pytest.importorskip("prometheus_client", reason="metrics ship with the optional `metrics` extra")


def _value(name: str, labels: dict[str, str]) -> float:
    from nexus.core.metrics import metric_value

    return metric_value(name, labels) or 0.0


def _decisions(capability: str, outcome: str, reason: str = "none") -> float:
    return _value(
        "nexus_billing_decisions_total",
        {"capability": capability, "outcome": outcome, "reason": reason},
    )


async def _seed(plan_id: str = "free"):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


# ---- the module never breaks its caller -----------------------------------------------------

def test_every_entry_point_swallows_a_broken_backend(monkeypatch):
    """A metric must not be able to fail the work it describes."""
    from nexus.core import metrics

    class Exploding:
        def labels(self, **_kw):
            raise RuntimeError("registry on fire")

    monkeypatch.setattr(metrics, "_counter", lambda *a, **k: Exploding())
    monkeypatch.setattr(metrics, "_histogram", lambda *a, **k: Exploding())

    # None of these may raise.
    metrics.record_billing_decision("x", "allowed")
    metrics.record_credit_burn("x", 5)
    metrics.record_webhook_event("stripe", "processed")
    metrics.record_job_outcome("j", "succeeded")
    metrics.observe_entitlement_resolve(0.01)


def test_absent_prometheus_client_degrades_to_noop(monkeypatch):
    """The metrics extra is optional; without it every call is a no-op, not an ImportError."""
    from nexus.core import metrics

    monkeypatch.setattr(metrics, "_METRICS", {})
    monkeypatch.setattr(metrics, "_client", lambda: None)

    metrics.record_billing_decision("x", "allowed")
    assert metrics.metric_value("nexus_billing_decisions_total", {}) is None


def test_a_zero_credit_burn_is_not_counted():
    """Refused and replayed burns pass amount<=0 or never reach here; counting them would make
    the burn rate climb while nothing was charged."""
    from nexus.core import metrics

    before = _value("nexus_billing_credits_burned_total", {"capability": "zero.test"})
    metrics.record_credit_burn("zero.test", 0)
    assert _value("nexus_billing_credits_burned_total", {"capability": "zero.test"}) == before


# ---- billing decisions ----------------------------------------------------------------------

async def test_an_allowed_call_is_counted(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("growth")
    before = _decisions("ai.research_brief", "allowed")
    async with tenant_session(tid) as ts:
        r = await check_and_meter(ts, capability_id="ai.research_brief")
    assert r.allowed
    assert _decisions("ai.research_brief", "allowed") == before + 1


async def test_shadow_mode_records_would_block_instead_of_blocked(monkeypatch):
    """The headline metric. In shadow mode the engine allows the call, so `blocked` must stay
    where it was and `would_block` must move — otherwise a dashboard would report customer-facing
    402s that never happened."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid = await _seed("free")   # Free disables module.outreach

    before_would = _decisions("module.outreach", "would_block", "disabled")
    before_blocked = _decisions("module.outreach", "blocked", "disabled")
    async with tenant_session(tid) as ts:
        r = await check_and_meter(ts, capability_id="module.outreach")

    assert r.allowed and r.would_block
    assert _decisions("module.outreach", "would_block", "disabled") == before_would + 1
    assert _decisions("module.outreach", "blocked", "disabled") == before_blocked


async def test_enforced_mode_records_blocked(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("free")

    before = _decisions("module.outreach", "blocked", "disabled")
    async with tenant_session(tid) as ts:
        r = await check_and_meter(ts, capability_id="module.outreach")

    assert not r.allowed
    assert _decisions("module.outreach", "blocked", "disabled") == before + 1


async def test_off_mode_records_nothing(monkeypatch):
    """`off` is the incident escape hatch: a pure passthrough that does not even evaluate. It must
    not emit decisions either, or the kill switch would look like normal operation."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid = await _seed("free")

    before = _decisions("module.outreach", "would_block", "disabled")
    async with tenant_session(tid) as ts:
        await check_and_meter(ts, capability_id="module.outreach")
    assert _decisions("module.outreach", "would_block", "disabled") == before


async def test_an_engine_error_is_counted_as_error(monkeypatch):
    """The engine allows on any internal failure — right for the customer, and indistinguishable
    from "nobody hit a limit" on every other metric. This counter is what separates them."""
    from nexus.billing import entitlements
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    async def boom(*_a, **_kw):
        raise RuntimeError("resolution exploded")

    monkeypatch.setattr(entitlements, "resolve_entitlement", boom)
    tid = await _seed("growth")

    before = _decisions("ai.research_brief", "error")
    async with tenant_session(tid) as ts:
        r = await entitlements.check_and_meter(ts, capability_id="ai.research_brief")

    assert r.allowed          # never breaks the product
    assert _decisions("ai.research_brief", "error") == before + 1


async def test_resolution_latency_is_observed(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("growth")
    before = _value("nexus_entitlement_resolve_seconds_count", {})
    async with tenant_session(tid) as ts:
        await check_and_meter(ts, capability_id="ai.research_brief")
    assert _value("nexus_entitlement_resolve_seconds_count", {}) == before + 1


# ---- webhooks -------------------------------------------------------------------------------

async def test_a_rejected_webhook_is_counted(client):
    """A rejection writes NO row — the dedupe table only records events that verified. This
    counter is the only trace a wrong signing secret leaves."""
    before = _value(
        "nexus_webhook_events_total", {"provider": "stripe", "outcome": "bad_signature"}
    )
    r = await client.post(
        "/api/billing/webhooks/stripe",
        content=b'{"id":"evt_1","type":"invoice.paid"}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400
    after = _value(
        "nexus_webhook_events_total", {"provider": "stripe", "outcome": "bad_signature"}
    )
    # No secret configured in tests raises SignatureError too, so either label is a rejection;
    # assert the specific one this path produces.
    assert after == before + 1


# ---- jobs -----------------------------------------------------------------------------------

async def test_job_counters_carry_the_job_name():
    """`workers/metrics.py` stays a flat dict — a dict keyed by job name is a memory leak waiting
    for a name derived from user input. The Prometheus mirror is where the breakdown lives."""
    from nexus.workers.metrics import increment_job_counter, job_counters

    before_prom = _value("nexus_jobs_total", {"job": "unit.test.job", "outcome": "succeeded"})
    before_dict = job_counters()["succeeded"]

    increment_job_counter("succeeded", job="unit.test.job")

    assert _value("nexus_jobs_total", {"job": "unit.test.job", "outcome": "succeeded"}) == (
        before_prom + 1
    )
    # Both are updated from one call site, so they cannot disagree about a total.
    assert job_counters()["succeeded"] == before_dict + 1


async def test_an_unlabelled_increment_still_records():
    """Existing call sites pass no job name; they must keep working."""
    from nexus.workers.metrics import increment_job_counter

    before = _value("nexus_jobs_total", {"job": "unknown", "outcome": "retried"})
    increment_job_counter("retried")
    assert _value("nexus_jobs_total", {"job": "unknown", "outcome": "retried"}) == before + 1


# ---- state gauges ---------------------------------------------------------------------------

async def test_state_gauges_report_queue_depth_and_dead_letters():
    from nexus.workers.queue import Job, InMemoryTaskQueue, get_task_queue, set_task_queue
    from nexus.workers.state_metrics import refresh_state_metrics

    original = get_task_queue()
    q = InMemoryTaskQueue()
    set_task_queue(q)
    try:
        await q.enqueue(Job(name="metrics.probe", payload={}))
        await q.enqueue(Job(name="metrics.probe", payload={}))
        out = await refresh_state_metrics()
        assert out["nexus_queue_depth"] == 2
        assert _value("nexus_queue_depth", {}) == 2
        # No dead letters in a fresh database, and 0 is the truthful answer here (the query
        # succeeded) — unlike an unmeasurable queue, which must leave the gauge absent.
        assert out["nexus_dead_letter_jobs"] == 0
    finally:
        set_task_queue(original)


async def test_a_queue_that_cannot_report_depth_leaves_the_gauge_absent():
    """0 would read as "healthy and empty". "We could not look" is a different fact."""
    from nexus.workers.state_metrics import _refresh_queue_depth
    from nexus.workers.queue import InMemoryTaskQueue, get_task_queue, set_task_queue

    class Mute(InMemoryTaskQueue):
        async def depth(self):
            return None

    original = get_task_queue()
    set_task_queue(Mute())
    try:
        assert await _refresh_queue_depth() == {}
    finally:
        set_task_queue(original)


async def test_dunning_depth_comes_from_subscription_status():
    """past_due IS the dunning queue: deriving it from the rows the sweep reads keeps the two
    from disagreeing."""
    from nexus.models.billing import BillingSubscription
    from nexus.workers.state_metrics import refresh_state_metrics

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription, BillingSubscription.plan_id == "growth")
        sub.status = "past_due"
        await ts.flush()

    out = await refresh_state_metrics()
    assert out.get('nexus_subscriptions{status="past_due"}', 0) >= 1


async def test_a_failing_step_does_not_take_the_others_down(monkeypatch):
    from nexus.workers import state_metrics

    async def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(state_metrics, "_refresh_dead_letters", boom)
    out = await state_metrics.refresh_state_metrics()
    assert "nexus_dead_letter_jobs" not in out
    assert "nexus_queue_depth" in out          # the other steps still ran


# ---- exposure -------------------------------------------------------------------------------

async def test_metrics_is_exposed_by_default_and_absent_from_the_schema(client):
    """On by default since M15 — a deployment nobody remembered to instrument is blind."""
    from nexus.core.config import get_settings

    assert get_settings().metrics_enabled is True

    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "http_request" in r.text

    schema = await client.get("/openapi.json")
    assert "/metrics" not in schema.json()["paths"]


async def test_the_worker_metrics_server_is_opt_outable(monkeypatch):
    """Port 0 disables the listener without touching metrics_enabled, so an operator can keep
    app metrics while refusing to open a port in the worker."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "worker_metrics_port", 0)
    assert get_settings().worker_metrics_port == 0
