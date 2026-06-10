# CRM Auto-Sync — Design Spec

**Sub-project E (improvement #5: Continuous / Automatic CRM Sync)**
Date: 2026-06-10
Status: Approved (ready for implementation plan)

## 1. Goal

Make NEXUS continuously and automatically push GTM state to the customer's CRM
(Salesforce / HubSpot) — scored accounts, relevance changes, the contact roster, and new
signals/alerts as engagement activity — **without a human clicking "push"**. Today CRM
outbound is manual (`POST /api/integrations/crm/push/{account_id}`) or fires only as a
side effect of a `crm_push` play action. There is no continuous reconciliation of account
state into the CRM. This sub-project adds an automatic sync that runs as another driver of
the Continuous-Automation heartbeat (sub-project D) plus an event-driven fast path, gated
per tenant and safe by default.

**Non-negotiable constraints:**
- The offline test path (SQLite + stub LLM + in-memory queue) must stay green and
  **zero-network**. Auto-sync is **off by default**; tests use the recording stub CRM
  connector (which buffers pushes in memory) so the sync path is fully exercised without
  any network.
- Every unit of work runs inside a `TenantSession` (RLS-scoped). No cross-tenant leakage.
- Production-grade at 1M-user scale: idempotent, restart-safe, multi-worker-safe,
  change-aware (never re-pushes unchanged accounts), cost-bounded.

## 2. Core insight — the connector and the heartbeat already exist

Two seams are already built:

1. **`nexus/ingestion/crm.py`** — a bi-directional `CRMConnector`. Outbound it exposes
   `push_account(account, *, contacts=…) -> CRMPushResult` (firmographics + contacts) and
   `push_activity(*, account_id, kind, detail=…) -> CRMPushResult` (an engagement-timeline
   event). `get_crm_connector()` resolves the configured provider (`stub|salesforce|hubspot`).
   The offline `StubCRMConnector` **records** pushes into in-memory buffers
   (`pushed_accounts` / `pushed_activities`) — this is the zero-network test substrate.
2. **`nexus/workers/scheduler.py::run_scheduler`** (sub-project D) — a heartbeat that
   enqueues idempotent driver jobs each tick while `automation_enabled`.

So CRM auto-sync is **not a new integration**. It is: one new change-detection column, one
shared "push this account" function, two thin handlers (a heartbeat sweep + a single-account
event job), one generic domain event, and gating. The actual CRM I/O reuses the existing
connector verbatim.

## 3. Architecture

```
  EVENT PATH (low latency)                      HEARTBEAT PATH (completeness backstop)
  ────────────────────────                      ──────────────────────────────────────
  process_account(ts, acct)                     run_scheduler tick (every tick, D):
    … score + ingest signals …                    if crm_sync_enabled:
    publish Event("account.scored") ─┐              enqueue sync_crm_due_accounts ─┐
                                      │                                            │
  on_account_scored subscriber  ◀────┘   handle_sync_crm_due_accounts ◀───────────┘
    if crm_sync_enabled:                   if not crm_sync_enabled: skip
      enqueue sync_crm_account(t, a) ─┐    global raw id-scan: accounts in tenants where
                                      │      automation_enabled AND DUE
  handle_sync_crm_account  ◀──────────┘      (crm_synced_at IS NULL OR updated_at > crm_synced_at),
    open TenantSession(t)                     ORDER BY crm_synced_at NULLS FIRST, LIMIT batch,
    re-check tenant.automation_enabled         FOR UPDATE SKIP LOCKED (Postgres)
    sync_account_to_crm(ts, acct) ──┐        group by tenant → per tenant TenantSession:
                                    │          for each acct: sync_account_to_crm(ts, acct) ─┐
                  ┌─────────────────┴───────────────────────────────────────────────────────┘
                  ▼
       sync_account_to_crm(ts, account, *, connector, now):   ← the single shared push unit
         contacts = ts.list(Contact, account_id == account.id)
         res = connector.push_account(account, contacts=contacts)
         if res.ok:
           if account.crm_synced_at is not None:            # no first-sync activity backfill
             for sig in signals with created_at > crm_synced_at:
               connector.push_activity(account_id=account.crm_id or account.id,
                                       kind="signal", detail={…})
           account.crm_synced_at = now                       # claim — excludes from next sweep
         return res
```

**Why hybrid (event + heartbeat):**
- The **event path** gives near-real-time sync the moment an account's score/signals change
  (the `account.scored` event fires at the end of `process_account`). It only *enqueues* a
  job — CRM I/O never runs on the hot path.
- The **heartbeat path** guarantees completeness: any account missed by the event path
  (worker restart, dropped event, never-scored-since-feature-launch) is swept up. It is a
  cheap no-op when nothing is due.
- Both converge on **one** idempotent, change-aware `sync_account_to_crm`, so there is a
  single source of truth for "what a sync does."

**Why `account.scored` on the EventBus (not a direct enqueue inside `process_account`):**
- Keeps the pipeline generic — `process_account` publishes a domain fact, it does not know
  about CRM.
- `account.scored` is **reused by sub-project E's Live Dashboard** for live account updates.
- It is the swap point for a real broker (Redis Streams / NATS) at scale — the envelope
  (`Event` with `run_id`/`causation_id`) is already broker-shaped.

## 4. Change detection — `crm_synced_at`, not an interval

An account is **due** when `crm_synced_at IS NULL OR updated_at > crm_synced_at`. Because
`TimestampMixin.updated_at` carries `onupdate=utcnow`, **any** mutation to an account row
(a rescore persists `composite`, an enrichment, a field edit) bumps `updated_at`, marking it
due. A successful push stamps `crm_synced_at = now`, so the account is excluded from the next
sweep until it changes again. This means **unchanged accounts are never re-pushed** — the
critical property for 1M-account scale (no periodic re-push storm, no CRM rate-limit blowups,
no noisy duplicate activity).

**Dialect-safety note:** `created_at`/`updated_at` use plain `DateTime` (naive on SQLite),
while `crm_synced_at` uses `TZDateTime` (always tz-aware). Therefore:
- **Due-selection** compares **column-vs-column in SQL** (`Account.updated_at >
  Account.crm_synced_at`) — both stored values, dialect-native, no Python/param mismatch.
- **"New signals since last sync"** is filtered **in Python** via `ensure_aware` over the
  account's signals (a bounded set per account), avoiding a naive-vs-aware bound-param
  comparison against SQLite string storage.

## 5. Components

### 5.1 Config knobs — `nexus/core/config.py`
Mirror the existing `automation_*` block:

```python
# CRM Auto-Sync (sub-project E): continuously push account state to the configured CRM.
# OFF by default (safe opt-in, like automation_enabled) so the suite is deterministic and
# zero-network (tests use the recording stub connector).
crm_sync_enabled: bool = False        # global master switch for auto-sync
crm_sync_batch_size: int = 100        # max accounts claimed per heartbeat sweep
```

No interval knob — change-detection is `updated_at`-driven, not time-driven.

### 5.2 Account change-detection column — `nexus/models/account.py`
Add to `Account`:

```python
# CRM Auto-Sync: when this account's state was last pushed to the CRM. NULL = never synced
# (always due). Stamped on a successful push; an account is due again only when updated_at
# moves past it. Indexed for the NULLS-FIRST due-selection scan.
crm_synced_at: Mapped[datetime | None] = mapped_column(
    TZDateTime(), nullable=True, index=True
)
```

### 5.3 Alembic migration `0009` — `migrations/versions/0009_*.py`
Adds `accounts.crm_synced_at` (timestamptz, nullable) + its index. **Note (matches
0005–0008):** the offline test path builds schema via `Base.metadata.create_all`, not
`alembic upgrade head`. The migration is for Postgres production; verified by inspection and
a revision-chain assertion (down_revision == `0008`), not by running `upgrade head` on SQLite.

### 5.4 Shared push unit — `nexus/ingestion/crm_sync.py` (NEW, co-located with the connector)
```python
async def sync_account_to_crm(
    ts: TenantSession, account: Account, *, connector: CRMConnector, now: datetime
) -> CRMPushResult:
    """Push one account's state to the CRM and stamp crm_synced_at on success.

    Pushes the account record + contact roster always; for an already-synced account also
    pushes one activity per signal ingested since the last sync. Never raises across the
    connector boundary (push_* return CRMPushResult). Idempotent: a no-op re-stamp if nothing
    changed (the caller only selects due accounts, so this is rarely reached redundantly)."""
    contacts = await ts.list(Contact, Contact.account_id == account.id)
    res = await connector.push_account(account, contacts=contacts)
    if res.ok:
        prior = account.crm_synced_at
        if prior is not None:  # no historical activity backfill on first sync (anti-flood)
            prior = ensure_aware(prior)
            sigs = await ts.list(SignalEvent, SignalEvent.account_id == account.id)
            for sig in sigs:
                if ensure_aware(sig.created_at) > prior:
                    await connector.push_activity(
                        account_id=account.crm_id or account.id,
                        kind="signal",
                        detail={"signal": sig.title, "kind": sig.kind, "source": sig.source},
                    )
        account.crm_synced_at = now
    return res
```

**v1 first-sync rule (documented):** a never-synced account (`crm_synced_at IS NULL`) pushes
the record + contacts but **no historical activity backfill** — avoids flooding the CRM
timeline with the account's entire signal history on first contact. Activity accrues from the
next sync onward. Uniform across both trigger paths; deterministic; no extra knob.

### 5.5 Event publication — `nexus/pipeline.py`
At the end of `process_account`, after scoring, publish a generic domain event:

```python
await get_event_bus().publish(
    Event(
        name="account.scored",
        tenant_id=ts.tenant_id,
        payload={
            "account_id": account.id,
            "composite_score": composite,
            "new_signals": len(new_signals),
        },
    )
)
```

Additive and backward-compatible: with no subscriber registered, `publish` is a no-op (the
bus returns early when a name has no handlers). Existing tests that dispatch `process_account`
are unaffected.

### 5.6 Subscriber — `nexus/ingestion/crm_sync.py`
```python
async def on_account_scored(event: Event) -> None:
    """Event fast-path: enqueue a single-account CRM sync when auto-sync is enabled.
    Authoritative gating lives in the handler; this is a cheap pre-filter."""
    if not get_settings().crm_sync_enabled:
        return
    await enqueue_sync_crm_account(event.tenant_id, event.payload["account_id"])


def register_crm_sync_subscribers(bus: EventBus | None = None) -> None:
    """Idempotent subscription of the CRM-sync fast-path to ``account.scored``."""
    bus = bus or get_event_bus()
    bus.subscribe("account.scored", on_account_scored)
```

### 5.7 Handlers + enqueue helpers — `nexus/workers/tasks.py`

`handle_sync_crm_account(payload: dict) -> dict` (event single-account):
1. `settings = get_settings()`; if not `crm_sync_enabled`: return `{"skipped": "crm_sync_disabled"}`.
2. `tid, aid = payload["tenant_id"], payload["account_id"]`.
3. `async with tenant_session(tid) as ts:` load the `Tenant` row; if not
   `tenant.automation_enabled`: return `{"skipped": "tenant_opted_out"}`.
4. Load the account (RLS-scoped); if gone, return `{"skipped": "account_missing"}`.
5. `res = await sync_account_to_crm(ts, account, connector=get_crm_connector(),
   now=datetime.now(timezone.utc))`. Return `{"account_id": aid, "ok": res.ok}`.

`handle_sync_crm_due_accounts(payload: dict) -> dict` (heartbeat sweep), mirrors
`handle_refresh_due_accounts`:
1. if not `settings.crm_sync_enabled`: return `{"skipped": "crm_sync_disabled"}`.
2. `now` (overridable via `payload["now_iso"]` for tests).
3. Raw tenant-agnostic session: select `(tenant_id, id)` of accounts joined to tenants where
   `Tenant.automation_enabled == True` AND `(Account.crm_synced_at IS NULL OR
   Account.updated_at > Account.crm_synced_at)`, `ORDER BY crm_synced_at NULLS FIRST`,
   `LIMIT crm_sync_batch_size`; on Postgres `.with_for_update(skip_locked=True, of=Account)`.
4. Group ids by tenant. For each tenant: `async with tenant_session(tid) as ts:` and for each
   account `await sync_account_to_crm(ts, account, connector=get_crm_connector(), now=now)`.
5. Return `{"tenants": n, "accounts": m}`.

Add `enqueue_sync_crm_account(tenant_id, account_id, *, queue=None)` and
`enqueue_sync_crm_due_accounts(*, queue=None)`; register
`"sync_crm_account"` and `"sync_crm_due_accounts"` in `HANDLERS`.

`sync_account_to_crm` is imported lazily inside the handlers (function-local import, matching
the existing handler style) to avoid an import cycle (`tasks` ↔ `crm_sync`).

### 5.8 Scheduler wiring — `nexus/workers/scheduler.py`
In the heartbeat tick, enqueue the CRM driver gated on its **own** switch (so no no-op job is
enqueued when auto-sync is entirely off):

```python
if settings.automation_enabled:
    await enqueue_advance_cadences(queue=queue)
    await enqueue_refresh_due_accounts(queue=queue)
if settings.crm_sync_enabled:
    await enqueue_sync_crm_due_accounts(queue=queue)
```

The heartbeat already runs whenever `automation_enabled`; the CRM sweep additionally requires
`crm_sync_enabled`. (The handler re-checks `crm_sync_enabled` regardless, so this is a
pre-filter, not the authority.)

### 5.9 Subscriber registration (two entrypoints)
The `EventBus` is a per-process singleton with no current subscribers. `account.scored` is
published in whichever process runs `process_account` — that is **both** the API process
(synchronous trigger via `nexus/api/routers/agents.py`) and the worker process (the
`process_account` job). So `register_crm_sync_subscribers()` is called from **both**:
- `nexus/main.py::lifespan` — on FastAPI startup (before `yield`).
- `nexus/workers/worker.py::_main` — on worker startup (before the `gather`).

This is the one-time plumbing cost of routing through the EventBus; it is justified because
`account.scored` is reused by the Live Dashboard and is the future broker swap point.

### 5.10 Gating — global switch + reuse `automation_enabled` (advisory non-stub)
A sync runs only when **`settings.crm_sync_enabled` (global) AND the tenant's
`automation_enabled` (per-tenant)**. No new tenant column — reuses the sub-project D opt-in
admins already control via `PATCH /api/tenant/automation`.

**Connector is advisory, not a hard gate.** The driver always uses `get_crm_connector()`. The
offline tests inject a recording stub via `set_crm_connector()` and assert its buffers — a
hard "non-stub only" gate would make the sync path untestable. In production, an admin pairs
`crm_sync_enabled=True` with a real `crm_provider` (`salesforce`/`hubspot`); pushing to the
stub is a harmless no-op. (Optional: log a warning when `env == "production"` and the resolved
connector is the stub.)

### 5.11 Observability endpoint — `nexus/api/routers/integrations.py`
`GET /api/integrations/crm/sync-status` (read-only, `Depends(require(Permission.manage_accounts))`):

```json
{ "enabled": true, "provider": "salesforce", "pending": 12, "synced": 988 }
```

- `enabled` = `settings.crm_sync_enabled AND tenant.automation_enabled`.
- `provider` = `settings.crm_provider`.
- `pending` = count of this tenant's due accounts (`crm_synced_at IS NULL OR updated_at >
  crm_synced_at`); `synced` = count of not-due. Both are **tenant-scoped** counts (RLS),
  never a global scan — O(tenant), safe at 1M total accounts. Pydantic `CRMSyncStatusOut`.

No PATCH — the on/off control is the existing automation toggle plus the global env switch.

## 6. Test plan (offline, zero-network, deterministic)

All tests force `crm_sync_enabled` explicitly and inject a recording stub CRM connector via
`set_crm_connector()`; they assert against `pushed_accounts` / `pushed_activities` buffers.
No real connector, no network.

1. **Driver gating:** `crm_sync_enabled=False` → `handle_sync_crm_due_accounts` returns
   `{"skipped": "crm_sync_disabled"}` and pushes nothing.
2. **Due-selection + claim:** seed accounts with varied state in an opted-in tenant — never
   synced (`crm_synced_at=NULL`), changed (`updated_at > crm_synced_at`), and fresh
   (`crm_synced_at >= updated_at`). Run the sweep; assert only NULL+changed accounts are
   pushed and stamped; fresh accounts untouched.
3. **Activity payload:** an already-synced account with new signals (`created_at > prior`) →
   `push_account` once + `push_activity` once per new signal. A never-synced account →
   `push_account` only, **no** activity backfill.
4. **Idempotency / no re-push:** run the sweep twice with the same `now` → the second run
   finds nothing due (stamps exclude them); buffers unchanged after the second run.
5. **Tenant isolation + automation gate:** tenant A `automation_enabled=True`, tenant B
   `False`; both have due accounts → only A's accounts are synced. B never touched.
6. **Global switch dominates:** `crm_sync_enabled=False` → both handlers return `skipped`
   even when a tenant is opted in.
7. **Event fast-path:** register subscribers; with `crm_sync_enabled=True`, publishing
   `account.scored` (or dispatching `process_account`) enqueues exactly one `sync_crm_account`
   job; dispatching it pushes the account to the recording stub. With `crm_sync_enabled=False`
   no job is enqueued.
8. **Scheduler enqueues the driver:** one scheduler iteration with `crm_sync_enabled=True`
   enqueues `sync_crm_due_accounts`; with `False` it does not.
9. **API status + RBAC:** a manager/admin (`manage_accounts`) can `GET /crm/sync-status` and
   sees correct `pending`/`synced` counts; a rep gets 403; counts are tenant-isolated.
10. **Migration chain:** `0009` exists with `down_revision == "0008"`.
11. **Full suite green:** existing 280 tests + the new ones all pass.

## 7. Out of scope (YAGNI)

- Bidirectional conflict resolution / CRM-wins-vs-NEXUS-wins merge policy (outbound only here;
  inbound stays the existing manual `POST /crm/sync`).
- Field-level mapping UI / per-tenant custom CRM field maps (the connector owns payload shape).
- Per-tenant `crm_sync_enabled` column or per-tenant interval overrides (single global switch
  + reuse of `automation_enabled` in v1).
- Real Salesforce/HubSpot wire implementations (the connector classes stay stubs until live
  credentials are wired; this sub-project delivers the *automatic* drive, provider-agnostic).
- Retry/backoff bookkeeping beyond the queue's existing retry path; `push_*` already never
  raise across the boundary, so a failed push simply leaves `crm_synced_at` unstamped → the
  account stays due and is retried on the next sweep (self-healing).
- Frontend UI for sync status (backend + API only; the Live Dashboard sub-project surfaces it).

## 8. Files touched

- `nexus/core/config.py` — 2 new knobs (`crm_sync_enabled`, `crm_sync_batch_size`).
- `nexus/models/account.py` — `Account.crm_synced_at` (`TZDateTime`, indexed).
- `migrations/versions/0009_crm_auto_sync.py` — new migration (down_revision `0008`).
- `nexus/ingestion/crm_sync.py` — NEW: `sync_account_to_crm`, `on_account_scored`,
  `register_crm_sync_subscribers`.
- `nexus/pipeline.py` — publish `account.scored` at the end of `process_account`.
- `nexus/workers/tasks.py` — `handle_sync_crm_account`, `handle_sync_crm_due_accounts`,
  `enqueue_sync_crm_account`, `enqueue_sync_crm_due_accounts`, two HANDLERS entries.
- `nexus/workers/scheduler.py` — enqueue `sync_crm_due_accounts` each tick (gated by
  `crm_sync_enabled`).
- `nexus/main.py` — `register_crm_sync_subscribers()` in `lifespan`.
- `nexus/workers/worker.py` — `register_crm_sync_subscribers()` in `_main`.
- `nexus/api/routers/integrations.py` — `GET /crm/sync-status` + `CRMSyncStatusOut` schema.
- `tests/test_crm_auto_sync.py` — NEW test module.
