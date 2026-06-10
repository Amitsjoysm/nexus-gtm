# Continuous Automation — Design Spec

**Sub-project D (improvement #2: Continuous / Autonomous Operation)**
Date: 2026-06-10
Status: Approved (ready for implementation plan)

## 1. Goal

Make NEXUS run continuously and autonomously, the way a real SDR works a territory:
sense new signals, rescore relevance, fire plays/alerts, and advance multi-touch
follow-up cadences — **without a human enqueuing jobs**. Today the worker is pull-only
(`dequeue → dispatch`); there is no periodic scheduler, so recurring work only happens
when something manually enqueues it. This sub-project adds a heartbeat that drives the
recurring GTM loop on a schedule, gated per tenant, and safe by default.

**Non-negotiable constraints:**
- The offline test path (SQLite + stub LLM + in-memory queue) must stay green and
  **zero-network**. The autonomous loop must be **off by default** so the suite is
  deterministic and never reaches out.
- Every unit of work runs inside a `TenantSession` (RLS-scoped). No cross-tenant leakage.
- Production-grade: idempotent, restart-safe, multi-worker-safe, cost-bounded at scale.

## 2. Core insight — the SDR loop already exists

`nexus/pipeline.py::process_account` already chains the entire sense→act loop for one
account:

```
ingest signals (run_sources) → score relevance (scoring agent, persist) →
create inbox tasks → evaluate plays → plays fire alerts / CRM push / SEP push
```

So the four recurring concerns requested — **signal ingestion refresh, relevance
rescoring, play evaluation, alerts** — are *all* delivered by periodically re-running
`process_account` per account. There is **no new "SDR engine" to build**.

The autonomous SDR therefore reduces to **two recurring tasks, both already implemented
as handlers/functions**:

1. `process_account(ts, account)` — refresh one account (sense→score→act).
2. `advance_cadences` — drive due cadence touches (the existing handler from sub-project C).

Continuous Automation is the **heartbeat that drives these two on a schedule**, plus the
account-selection logic that keeps it cost-bounded, plus per-tenant opt-in control.

## 3. Architecture

```
                         worker process (python -m nexus.workers.worker)
   ┌───────────────────────────────────────────────────────────────────────┐
   │  run_scheduler (NEW)            run_worker (UNCHANGED)                  │
   │  ── heartbeat coroutine ──      ── pull loop ──                         │
   │  every tick, if enabled:        while not stop:                         │
   │    enqueue advance_cadences  ─┐    job = dequeue()                      │
   │    enqueue refresh_due_accts ─┼──▶ dispatch(job)  ──▶ HANDLERS[...]     │
   │  stop.wait(tick_interval)     │                                         │
   └───────────────────────────────┼─────────────────────────────────────────┘
                                    │
   refresh_due_accounts handler (NEW), gated by automation_enabled:
     global raw scan → accounts in OPTED-IN tenants that are stale
       (last_refreshed_at IS NULL OR <= now - account_refresh_interval_s),
       NULLs first, LIMIT batch, FOR UPDATE SKIP LOCKED on Postgres
     group by tenant → per tenant: open TenantSession,
       stamp last_refreshed_at = now (claim), enqueue process_account(acct)
```

**Why queue-mediated (heartbeat enqueues driver jobs) rather than running work inline:**
- Scheduling stays decoupled from execution. The heartbeat is a tiny timer; a slow
  account never blocks the next tick.
- Reuses the existing queue / `dispatch` / retry / logging path verbatim.
- Each account refresh is an independent, idempotent, retryable unit (`process_account`
  is already a handler).
- Multi-worker-safe via the queue's `FOR UPDATE SKIP LOCKED` claiming (Postgres), exactly
  like the cadence advance tick.

**Why both drivers are enqueued every tick (no per-job interval bookkeeping in v1):**
Both drivers are idempotent and self-filtering. `advance_cadences` only touches due
enrollments; `refresh_due_accounts` only touches stale accounts and is a cheap no-op
when nothing is due. Enqueuing both each tick is therefore safe and keeps the scheduler
trivial. Per-job intervals can be layered on later if needed (YAGNI now).

## 4. Components

### 4.1 Config knobs — `nexus/core/config.py`
Mirror the existing `cadence_*` block (lines 77-80):

```python
# Continuous Automation (sub-project D): autonomous heartbeat that drives the recurring
# GTM loop (account refresh + cadence advance). OFF by default (safe opt-in, like
# cadence_enabled) so the test suite is deterministic and zero-network.
automation_enabled: bool = False            # global master switch for the heartbeat
automation_tick_interval_s: int = 60        # heartbeat period (seconds)
account_refresh_interval_s: int = 21600     # staleness threshold before re-processing (6h)
account_refresh_batch_size: int = 100       # max accounts claimed per tick across tenants
```

`automation_enabled` is the **global** kill switch. A tenant is processed only when BOTH
`settings.automation_enabled` is True AND that tenant's `automation_enabled` column is True.

### 4.2 Per-tenant opt-in — `nexus/models/identity.py`
Add to `Tenant` (the global, non-RLS isolation-boundary table the heartbeat scans):

```python
automation_enabled: Mapped[bool] = mapped_column(default=False)
```

Default False: a tenant runs autonomously only after an admin opts in.

### 4.3 Account staleness — `nexus/models/account.py`
Add to `Account`:

```python
last_refreshed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True, index=True
)
```

`NULL` = never refreshed = always due (NULLs are selected first). Stamped to `now` when
the refresh driver claims the account, so the next tick won't re-select it until the
staleness window elapses.

### 4.4 Alembic migration `0008` — `migrations/versions/0008_*.py`
Adds `tenants.automation_enabled` (bool, server_default false, not null) and
`accounts.last_refreshed_at` (timestamptz, nullable) + its index.
**Note (matches 0005/0006/0007):** the offline test path builds schema via
`Base.metadata.create_all`, not `alembic upgrade head` (because `0001_initial` uses
`create_all`). The migration is for Postgres production; it is verified by inspection /
isolation testing, not by running `upgrade head` on a fresh SQLite DB.

### 4.5 Refresh driver handler — `nexus/workers/tasks.py`
`handle_refresh_due_accounts(payload: dict) -> dict`:
1. `settings = get_settings()`; if not `settings.automation_enabled`: return
   `{"skipped": "automation_disabled"}`.
2. `now = datetime.now(timezone.utc)` (overridable via `payload["now_iso"]` for tests).
3. Raw tenant-agnostic session: select `(tenant_id, id)` of accounts joined to tenants
   where `Tenant.automation_enabled == True` AND
   `(Account.last_refreshed_at IS NULL OR Account.last_refreshed_at <= now - interval)`,
   `ORDER BY last_refreshed_at NULLS FIRST`, `LIMIT account_refresh_batch_size`.
   On Postgres add `.with_for_update(skip_locked=True)` (gated by `settings.is_postgres`).
   This raw read only reads ids — the per-tenant work below stays RLS-scoped.
4. Group selected account ids by tenant_id.
5. For each tenant: `async with tenant_session(tid) as ts:` load those accounts
   (RLS-scoped), set `account.last_refreshed_at = now`, and
   `await enqueue_process_account(tid, account.id)` for each.
6. Return `{"tenants": n, "accounts": m}`.

Add `enqueue_refresh_due_accounts(*, queue=None)` helper and register
`"refresh_due_accounts": handle_refresh_due_accounts` in `HANDLERS`.

Reuses the existing `enqueue_process_account` helper and `handle_process_account` handler
unchanged.

### 4.6 Heartbeat scheduler — `nexus/workers/scheduler.py` (NEW)
```python
async def run_scheduler(
    *, stop: asyncio.Event | None = None, queue: TaskQueue | None = None
) -> None:
    """Periodic heartbeat: enqueue the recurring drivers each tick while enabled.
    Inert (loops but enqueues nothing) unless automation_enabled."""
    stop = stop or asyncio.Event()
    queue = queue or get_task_queue()
    while not stop.is_set():
        settings = get_settings()
        if settings.automation_enabled:
            await enqueue_advance_cadences(queue=queue)
            await enqueue_refresh_due_accounts(queue=queue)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.automation_tick_interval_s)
        except asyncio.TimeoutError:
            pass  # tick elapsed; loop again
```

`stop.wait()` with a timeout makes the scheduler promptly cancellable (no fixed `sleep`
that delays shutdown).

### 4.7 Worker wiring — `nexus/workers/worker.py`
`_main` runs the pull loop and the heartbeat concurrently on one shared `stop` event:

```python
await asyncio.gather(
    run_worker(stop=stop),
    run_scheduler(stop=stop),
)
```

`run_worker` is **unchanged** — it stays a pure `dequeue → dispatch` loop. The scheduler
is a separate coroutine, cleanly decoupled.

### 4.8 Per-tenant toggle API — `nexus/api/routers/workspace.py`
- `GET  /api/tenant/automation` → `{"automation_enabled": bool}`
- `PATCH /api/tenant/automation` body `{"automation_enabled": bool}` → updated value

Both gated by `Depends(require(Permission.manage_workspace))` (admin+, the existing
admin-settings gate). Loads the current `Tenant` row by `ts.tenant_id`, reads/sets the
flag. Pydantic `AutomationSettingsIn` / `AutomationSettingsOut`.

## 5. Test plan (offline, zero-network, deterministic)

All tests run with the in-memory queue + stub LLM. `automation_enabled` is forced
explicitly per test (never relying on ambient env).

1. **Scheduler gating:** with `automation_enabled=False`, one scheduler iteration enqueues
   nothing; with `True`, it enqueues exactly `advance_cadences` + `refresh_due_accounts`.
   (Test by running a single iteration with an already-set `stop`, or a fake queue capturing
   `enqueue` calls.)
2. **Refresh selection + claim:** seed accounts with varied `last_refreshed_at`
   (NULL, fresh, stale) in an opted-in tenant; run `handle_refresh_due_accounts` with an
   injected `now`; assert only NULL/stale accounts are stamped and get a `process_account`
   job enqueued; fresh accounts untouched.
3. **Idempotency / no stampede:** running the refresh driver twice with the same `now`
   processes the stale set once (second run finds nothing newly due).
4. **Tenant isolation:** tenant A `automation_enabled=True`, tenant B `False`; both have
   stale accounts → only A's accounts are refreshed/enqueued. B is never touched.
5. **Global switch dominates:** `settings.automation_enabled=False` → driver returns
   `skipped` even if a tenant flag is True.
6. **API toggle + RBAC:** admin can `PATCH /api/tenant/automation`; a rep gets 403;
   `GET` reflects the set value; isolation across tenants.
7. **Full suite green:** existing 265 tests + the new ones all pass.

## 6. Out of scope (YAGNI)

- Generic per-tenant rule/trigger engine or user-defined automations.
- Per-tenant interval overrides (single global interval in v1).
- Time-based play triggers (plays stay signal-driven; periodic refresh re-feeds signals).
- Distributed leader election across worker processes — the queue's `SKIP LOCKED`
  claiming already makes the drivers multi-worker-safe.
- Frontend UI for the automation toggle (backend + API only; UI built later with
  `impeccable`, like the cadence UI).

## 7. Files touched

- `nexus/core/config.py` — 4 new knobs.
- `nexus/models/identity.py` — `Tenant.automation_enabled`.
- `nexus/models/account.py` — `Account.last_refreshed_at`.
- `migrations/versions/0008_*.py` — new migration (alembic revision after `0007_cadence`).
- `nexus/workers/tasks.py` — `handle_refresh_due_accounts` + `enqueue_refresh_due_accounts` + HANDLERS entry.
- `nexus/workers/scheduler.py` — NEW, `run_scheduler`.
- `nexus/workers/worker.py` — gather scheduler with worker in `_main`.
- `nexus/api/routers/workspace.py` — automation GET/PATCH + schemas.
- `tests/test_continuous_automation.py` — NEW test module.
