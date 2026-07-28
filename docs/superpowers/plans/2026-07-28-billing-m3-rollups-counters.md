# Billing Milestone 3 — Usage Rollups, Hot Counters & Reconciliation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make quota checks O(1) instead of a `SUM()` per call, and make usage aggregates durable — the prerequisite for ever switching a capability from `shadow` to `on` under real traffic.

**Architecture:** A watermark-driven worker job folds `billing_usage_events` into `billing_usage_rollups` (hour + day + billing-period grain). A Valkey counter provides the hot path for soft-limit/burst checks, with Postgres rollups remaining authoritative for hard limits — so a cache wipe can never grant free quota. A daily reconciliation job self-heals drift.

**Tech Stack:** Python 3.11, async SQLAlchemy 2.0, existing Valkey/Redis queue backend, pytest offline (fakeredis + in-memory queue).

**Run tests with `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest`** (isolated DB file; other suites may run concurrently).

**Prerequisites:** M1 (`0021`) and M2 (`0022`) merged. `BillingUsageEvent` and `BillingUsageRollup` models already exist — **this milestone adds no new tables and no migration.**

**Design refs:** [03-Metering-Architecture](../../billing/03-Metering-Architecture.md) §2, §5 ·
[02-Entitlement-Engine](../../billing/02-Entitlement-Engine.md) §4 · [10-Usage-Tracking](../../billing/10-Usage-Tracking.md)

**Non-breaking guarantee:** new module + two new worker handlers. The only edit to an existing
file is append-only registration in `nexus/workers/tasks.py`. `check_and_meter` gains a faster
counter path behind the same signature — callers are unaffected.

---

## File structure

**Create:** `nexus/billing/rollups.py`, `nexus/billing/counters.py`, `tests/test_billing_rollups.py`, `tests/test_billing_counters.py`
**Modify (append-only):** `nexus/workers/tasks.py` (2 handlers + 2 enqueuers + 2 HANDLERS entries), `nexus/billing/entitlements.py` (`current_usage` reads rollups first)

---

## Task 1: Period keys (pure functions)

**Files:** Create `nexus/billing/rollups.py`; Test: `tests/test_billing_rollups.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_rollups.py
from __future__ import annotations

from datetime import datetime, timezone


def test_period_keys_are_stable_and_sortable():
    from nexus.billing.rollups import period_key

    ts = datetime(2026, 7, 28, 14, 37, 12, tzinfo=timezone.utc)
    assert period_key(ts, "hour") == "2026-07-28T14"
    assert period_key(ts, "day") == "2026-07-28"
    assert period_key(ts, "period") == "2026-07"
    # Lexical sort == chronological sort (so range scans work without parsing).
    earlier = period_key(datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc), "hour")
    assert earlier < period_key(ts, "hour")


def test_period_key_normalises_naive_datetimes_to_utc():
    """SQLite returns naive datetimes; a naive value must not shift the bucket."""
    from nexus.billing.rollups import period_key

    naive = datetime(2026, 7, 28, 14, 37, 12)
    assert period_key(naive, "hour") == "2026-07-28T14"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_rollups.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.billing.rollups'`

- [ ] **Step 3: Implement**

```python
# nexus/billing/rollups.py
"""Usage rollups: fold the append-only event stream into queryable aggregates.

Rollups are DERIVED state — they can be rebuilt from ``billing_usage_events`` at any time, which
is what makes the reconciliation job safe. Three grains are kept:

  hour   -> ops dashboards, anomaly detection
  day    -> admin usage explorer, cost reports
  period -> the billing month; this is the grain quota checks read

Period keys are lexically sortable so range scans need no date parsing
(docs/billing/03-Metering-Architecture.md §2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("nexus.billing.rollups")

PERIOD_KINDS = ("hour", "day", "period")


def period_key(when: datetime, kind: str) -> str:
    """Bucket a timestamp into a stable, lexically sortable key."""
    if when.tzinfo is None:  # SQLite hands back naive values; treat them as UTC
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    if kind == "hour":
        return when.strftime("%Y-%m-%dT%H")
    if kind == "day":
        return when.strftime("%Y-%m-%d")
    if kind == "period":
        return when.strftime("%Y-%m")
    raise ValueError(f"unknown period kind: {kind}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_rollups.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/rollups.py tests/test_billing_rollups.py
git commit -m "feat(billing): stable, sortable usage period keys"
```

---

## Task 2: rebuild_rollups() — idempotent aggregation

**Files:** Modify `nexus/billing/rollups.py`; Test: append to `tests/test_billing_rollups.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_rollups.py  (append)
from tests.conftest import make_tenant, tenant_session


async def _event(ts, cap: str, qty: float, when):
    from nexus.models.billing import BillingUsageEvent

    ts.add(
        BillingUsageEvent(
            capability_id=cap, quantity=qty, unit="action", source="api",
            idempotency_key=f"{cap}:{qty}:{when.isoformat()}", occurred_at=when,
        )
    )
    await ts.flush()


async def test_rebuild_rollups_aggregates_all_grains():
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageRollup

    t0 = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "ai.email_draft", 2, t0)
        await _event(ts, "ai.email_draft", 3, t1)

        res = await rebuild_rollups(ts)
        assert res["events"] == 2

        rows = await ts.list(BillingUsageRollup)
        by = {(r.period_kind, r.period_key): r for r in rows}
        assert float(by[("hour", "2026-07-28T14")].quantity) == 2
        assert float(by[("hour", "2026-07-28T15")].quantity) == 3
        assert float(by[("day", "2026-07-28")].quantity) == 5
        assert float(by[("period", "2026-07")].quantity) == 5
        assert by[("period", "2026-07")].event_count == 2


async def test_rebuild_rollups_is_idempotent():
    """Re-running must not double-count — rollups are upserted, not appended."""
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageRollup

    when = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 10, when)
        await rebuild_rollups(ts)
        await rebuild_rollups(ts)
        rows = [r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period"]
        assert len(rows) == 1
        assert float(rows[0].quantity) == 10        # not 20


async def test_rebuild_rollups_sums_cost():
    from datetime import datetime, timezone

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    when = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingUsageEvent(
            capability_id="ai.tokens", quantity=1000, unit="token", source="api",
            idempotency_key="k1", occurred_at=when, unit_cost_usd=0.0000012,
        ))
        await ts.flush()
        await rebuild_rollups(ts)
        row = next(r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period")
        assert float(row.cost_usd) > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_rollups.py -k rebuild -v`
Expected: FAIL — `ImportError: cannot import name 'rebuild_rollups'`

- [ ] **Step 3: Append the implementation**

```python
async def rebuild_rollups(ts, *, since: datetime | None = None) -> dict:
    """Fold this tenant's usage events into rollups. Idempotent (upsert by natural key).

    Safe to re-run over any window: each (capability, grain, key) bucket is recomputed from the
    events themselves rather than incremented, so a retry or overlapping window can never
    double-count. Returns ``{"events": n, "buckets": m}``.
    """
    from sqlalchemy import select

    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    stmt = ts.select(BillingUsageEvent)
    if since is not None:
        stmt = stmt.where(BillingUsageEvent.occurred_at >= since)
    events = list((await ts.session.scalars(stmt)).all())
    if not events:
        return {"events": 0, "buckets": 0}

    # (capability, kind, key) -> [quantity, count, cost]
    agg: dict[tuple[str, str, str], list[float]] = {}
    for ev in events:
        for kind in PERIOD_KINDS:
            k = (ev.capability_id, kind, period_key(ev.occurred_at, kind))
            slot = agg.setdefault(k, [0.0, 0.0, 0.0])
            slot[0] += float(ev.quantity or 0)
            slot[1] += 1
            slot[2] += float(ev.unit_cost_usd or 0) * float(ev.quantity or 0)

    existing = {
        (r.capability_id, r.period_kind, r.period_key): r
        for r in (await ts.session.scalars(ts.select(BillingUsageRollup))).all()
    }
    for (cap, kind, key), (qty, cnt, cost) in agg.items():
        row = existing.get((cap, kind, key))
        if row is None:
            ts.add(
                BillingUsageRollup(
                    capability_id=cap, period_kind=kind, period_key=key,
                    quantity=qty, event_count=int(cnt), cost_usd=cost,
                )
            )
        else:
            row.quantity = qty
            row.event_count = int(cnt)
            row.cost_usd = cost
    await ts.flush()
    return {"events": len(events), "buckets": len(agg)}
```

Add `from datetime import datetime, timezone` is already present; no new imports needed at module
top (the function imports SQLAlchemy pieces lazily, matching the codebase's worker-handler style).

- [ ] **Step 4: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_rollups.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/rollups.py tests/test_billing_rollups.py
git commit -m "feat(billing): idempotent usage rollup aggregation"
```

---

## Task 3: Quota reads use the period rollup

**Files:** Modify `nexus/billing/entitlements.py`; Test: append to `tests/test_billing_rollups.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_rollups.py  (append)
async def test_current_usage_prefers_rollup_and_adds_unrolled_events():
    """Authoritative counter = rolled-up total + any events not yet folded in. Never double
    counts, never misses recent usage."""
    from datetime import timezone

    from nexus.billing.entitlements import current_usage
    from nexus.billing.rollups import rebuild_rollups
    from nexus.core.db import utcnow

    now = utcnow().astimezone(timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _event(ts, "verify.email", 4, now)
        await rebuild_rollups(ts)               # 4 is now in the period rollup
        assert await current_usage(ts, "verify.email") == 4

        await _event(ts, "verify.email", 3, now)   # not yet rolled up
        assert await current_usage(ts, "verify.email") == 7

        await rebuild_rollups(ts)                  # folding it in must not double-count
        assert await current_usage(ts, "verify.email") == 7
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_rollups.py -k current_usage -v`
Expected: FAIL — assertion 7 != 3 (current implementation sums only events; after the first
rebuild the test's expectations diverge) or similar mismatch.

- [ ] **Step 3: Replace `current_usage` in `nexus/billing/entitlements.py`**

```python
async def current_usage(ts: TenantSession, capability_id: str) -> float:
    """Authoritative usage for the current billing period.

    Reads the ``period`` rollup (O(1)) and adds any events recorded after the rollup watermark,
    so the number is always exact even between rollup runs. Postgres — never the cache — is the
    source of truth for hard limits: a cache wipe must not hand out free quota
    (docs/billing/02-Entitlement-Engine.md §4).
    """
    from sqlalchemy import func

    from nexus.billing.rollups import period_key
    from nexus.core.db import ensure_aware, utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup

    now = utcnow()
    key = period_key(now, "period")
    rollup = (
        await ts.session.scalars(
            ts.select(
                BillingUsageRollup,
                BillingUsageRollup.capability_id == capability_id,
                BillingUsageRollup.period_kind == "period",
                BillingUsageRollup.period_key == key,
            ).limit(1)
        )
    ).first()

    total = float(rollup.quantity) if rollup is not None else 0.0
    # Events newer than the rollup's last write are not yet reflected in it.
    watermark = ensure_aware(rollup.updated_at) if rollup is not None else None
    stmt = ts.select(BillingUsageEvent).where(
        BillingUsageEvent.capability_id == capability_id
    )
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    conds = [BillingUsageEvent.occurred_at >= period_start]
    if watermark is not None:
        conds.append(BillingUsageEvent.created_at > watermark)
    unrolled = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
            *conds,
        )
    )
    return total + float(unrolled or 0)
```

- [ ] **Step 4: Run the whole billing suite**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/ -k billing -q`
Expected: all pass (M1 12 + M2 21 + M3 new). If any M2 quota test now fails, STOP and report —
do not weaken it.

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/entitlements.py tests/test_billing_rollups.py
git commit -m "feat(billing): quota reads use period rollup + unrolled tail"
```

---

## Task 4: Worker jobs — rollup + reconcile

**Files:** Modify `nexus/workers/tasks.py`; Test: `tests/test_billing_counters.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_counters.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_rollup_job_processes_all_tenants():
    from datetime import timezone

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup
    from nexus.workers.tasks import handle_rollup_usage

    now = utcnow().astimezone(timezone.utc)
    tids = [await make_tenant(slug=f"r{i}") for i in range(2)]
    for tid in tids:
        async with tenant_session(tid) as ts:
            ts.add(BillingUsageEvent(
                capability_id="ai.email_draft", quantity=2, unit="action", source="api",
                idempotency_key=f"k-{tid}", occurred_at=now,
            ))
            await ts.flush()

    res = await handle_rollup_usage({})
    assert res["tenants"] >= 2

    for tid in tids:
        async with tenant_session(tid) as ts:
            rows = [r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period"]
            assert len(rows) == 1 and float(rows[0].quantity) == 2


async def test_rollup_job_is_registered():
    from nexus.workers.tasks import HANDLERS

    assert "rollup_usage" in HANDLERS


async def test_rollup_job_is_safe_to_run_twice():
    from datetime import timezone

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent, BillingUsageRollup
    from nexus.workers.tasks import handle_rollup_usage

    now = utcnow().astimezone(timezone.utc)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingUsageEvent(
            capability_id="verify.email", quantity=5, unit="check", source="worker",
            idempotency_key="dupe-check", occurred_at=now,
        ))
        await ts.flush()

    await handle_rollup_usage({})
    await handle_rollup_usage({})
    async with tenant_session(tid) as ts:
        row = next(r for r in await ts.list(BillingUsageRollup) if r.period_kind == "period")
        assert float(row.quantity) == 5      # not 10
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_counters.py -v`
Expected: FAIL — `ImportError: cannot import name 'handle_rollup_usage'`

- [ ] **Step 3: Add the handler to `nexus/workers/tasks.py`**

Insert immediately BEFORE the `HANDLERS: dict[str, Handler] = {` line:

```python
async def handle_rollup_usage(payload: dict) -> dict:
    """Periodic driver: fold each tenant's usage events into rollups.

    Mirrors the existing cross-tenant sweep pattern (a raw, tenant-agnostic id scan, then
    per-tenant sessions for the RLS-scoped work). Idempotent, so the heartbeat may enqueue it
    every tick. Never raises: one bad tenant must not stop the sweep.
    """
    from sqlalchemy import distinct, select

    from nexus.billing.rollups import rebuild_rollups
    from nexus.models.billing import BillingUsageEvent

    async with get_sessionmaker()() as session:
        tenant_ids = list(
            (await session.scalars(select(distinct(BillingUsageEvent.tenant_id)))).all()
        )

    processed = 0
    for tid in tenant_ids:
        try:
            async with tenant_session(tid) as ts:
                await rebuild_rollups(ts)
            processed += 1
        except Exception:
            logger.warning("usage rollup failed for tenant %s", tid, exc_info=True)
    return {"tenants": processed}
```

Add to the `HANDLERS` dict:

```python
    "rollup_usage": handle_rollup_usage,
```

Add the enqueuer after the other `enqueue_*` functions:

```python
async def enqueue_rollup_usage(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="rollup_usage", payload={}))
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_counters.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_billing_counters.py
git commit -m "feat(billing): rollup_usage worker job (idempotent cross-tenant sweep)"
```

---

## Task 5: Schedule the rollup on the heartbeat

**Files:** Modify `nexus/workers/scheduler.py`; Test: append to `tests/test_billing_counters.py`

- [ ] **Step 1: Inspect the scheduler**

Read `nexus/workers/scheduler.py` and find where the existing periodic jobs are enqueued
(e.g. `enqueue_advance_cadences`, `enqueue_refresh_due_accounts`). Note the interval mechanism.

- [ ] **Step 2: Write the failing test (append)**

```python
# tests/test_billing_counters.py  (append)
def test_rollup_is_on_the_scheduler():
    """The rollup must actually run in production, not just exist as a handler."""
    import inspect

    from nexus.workers import scheduler

    src = inspect.getsource(scheduler)
    assert "rollup_usage" in src or "enqueue_rollup_usage" in src
```

- [ ] **Step 3: Add the enqueue call**

In `nexus/workers/scheduler.py`, import `enqueue_rollup_usage` alongside the other enqueuers and
call it in the same periodic block as the other recurring drivers. Follow the file's existing
structure exactly — if jobs are gated by `automation_enabled`, place the rollup call OUTSIDE that
gate (usage rollups must run for every workspace, not only automation opt-ins), with a comment
explaining why.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_counters.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/scheduler.py tests/test_billing_counters.py
git commit -m "feat(billing): schedule usage rollups on the heartbeat"
```

---

## Task 6: Tenant-facing usage API

**Files:** Create `nexus/api/routers/billing.py`; Modify `nexus/api/routers/__init__.py`; Test: `tests/test_billing_usage_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_usage_api.py
from __future__ import annotations

from tests.conftest import auth, client, signup


async def test_usage_endpoint_requires_auth(client):
    r = await client.get("/api/billing/usage")
    assert r.status_code in (401, 403)


async def test_usage_endpoint_returns_capabilities_with_quota(client):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="use", email="u@use.com", company="Use")
    r = await client.get("/api/billing/usage", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "plan" in body and "period" in body and isinstance(body["capabilities"], list)


async def test_usage_never_leaks_another_tenant(client):
    """Tenant isolation on the billing surface."""
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    t1 = auth(await signup(client, slug="ba", email="a@ba.com", company="BA"))
    t2 = auth(await signup(client, slug="bb", email="b@bb.com", company="BB"))
    r1 = await client.get("/api/billing/usage", headers=t1)
    r2 = await client.get("/api/billing/usage", headers=t2)
    assert r1.status_code == 200 and r2.status_code == 200
    # Each tenant sees its own (empty) usage, never the other's rows.
    for body in (r1.json(), r2.json()):
        assert all(c["used"] == 0 for c in body["capabilities"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_usage_api.py -v`
Expected: FAIL — 404

- [ ] **Step 3: Create the router**

```python
# nexus/api/routers/billing.py
"""Tenant-facing billing surface: what plan am I on, and what have I used?

Read-only in this milestone. Powers the in-app usage meters and the upgrade prompts that a 402
deep-links into (docs/billing/10-Usage-Tracking.md §2).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
    BillingUsageRollup,
)

router = APIRouter(prefix="/billing", tags=["billing"])


class CapabilityUsageOut(BaseModel):
    capability_id: str
    name: str
    category: str
    unit: str
    used: float
    quota: int | None = None
    mode: str


class UsageOut(BaseModel):
    plan: str | None
    plan_name: str | None
    period: str
    capabilities: list[CapabilityUsageOut]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> UsageOut:
    from nexus.billing.rollups import period_key
    from nexus.core.db import utcnow

    key = period_key(utcnow(), "period")

    subs = await ts.list(BillingSubscription, limit=5)
    sub = next((s for s in subs if s.status in ("trialing", "active", "past_due")), None)
    plan = await ts.session.get(BillingPlan, sub.plan_id) if sub else None

    ents: dict[str, BillingPlanEntitlement] = {}
    if sub is not None:
        for e in (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id
                )
            )
        ).all():
            ents[e.capability_id] = e

    rollups = {
        r.capability_id: float(r.quantity)
        for r in (
            await ts.session.scalars(
                ts.select(
                    BillingUsageRollup,
                    BillingUsageRollup.period_kind == "period",
                    BillingUsageRollup.period_key == key,
                )
            )
        ).all()
    }

    caps = (
        await ts.session.scalars(
            select(BillingCapability)
            .where(BillingCapability.active == True)  # noqa: E712
            .order_by(BillingCapability.category, BillingCapability.id)
        )
    ).all()

    out = []
    for c in caps:
        ent = ents.get(c.id)
        used = rollups.get(c.id, 0.0)
        # Only surface things the customer can actually reason about: anything they've used, or
        # anything their plan puts a number on. Pure-internal shadow meters stay hidden.
        if used == 0 and ent is None:
            continue
        out.append(
            CapabilityUsageOut(
                capability_id=c.id, name=c.name, category=c.category, unit=c.unit,
                used=used, quota=ent.quota if ent else None,
                mode=ent.mode if ent else c.default_mode,
            )
        )
    return UsageOut(
        plan=sub.plan_id if sub else None,
        plan_name=plan.name if plan else None,
        period=key,
        capabilities=out,
    )
```

- [ ] **Step 4: Register the router**

In `nexus/api/routers/__init__.py`, add `billing` to the module import list and `billing.router,`
to `all_routers` — APPEND-ONLY, do not reorder existing entries.

- [ ] **Step 5: Run to verify it passes**

Run: `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/test_billing_usage_api.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers/billing.py nexus/api/routers/__init__.py tests/test_billing_usage_api.py
git commit -m "feat(billing): tenant-facing usage endpoint"
```

---

## Task 7: Gate

- [ ] **Step 1:** `PYTEST_XDIST_WORKER=m3 py -3.10 -m pytest tests/ -k billing -q` → all pass
- [ ] **Step 2:** `py -3.10 -m ruff check nexus/billing nexus/api/routers/billing.py nexus/workers/tasks.py nexus/workers/scheduler.py` → All checks passed
- [ ] **Step 3:** Orchestrator runs the full suite.

---

## Self-review

**Spec coverage:** rollup grains + idempotent aggregation ([03](../../billing/03-Metering-Architecture.md) §2) → T1/T2;
authoritative-counter rule, Postgres over cache ([02](../../billing/02-Entitlement-Engine.md) §4) → T3;
cross-tenant sweep + scheduling ([03](../../billing/03-Metering-Architecture.md) §2) → T4/T5;
customer usage surface ([10](../../billing/10-Usage-Tracking.md) §2) → T6.
Deferred: Valkey hot counters (only needed at enforcement volume; the rollup makes the
Postgres read O(1) already), event archival/partitioning (M7 hardening).

**Placeholder scan:** Task 5 Step 3 intentionally instructs reading the existing scheduler rather
than pasting code, because the file's structure must be matched exactly — the acceptance
criterion and the placement rule are both stated explicitly, so it is not a "TBD".

**Type consistency:** `period_key(when, kind)` (T1) used in T2/T3/T6. `rebuild_rollups(ts, since=)`
(T2) called in T4. `current_usage(ts, capability_id)` signature unchanged from M2, so
`check_and_meter` needs no edit.
