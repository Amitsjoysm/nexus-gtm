# Continuous Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an autonomous heartbeat to the NEXUS worker process that periodically drives the recurring GTM loop — account refresh (`process_account`) and cadence advance (`advance_cadences`) — for tenants that opt in, with no manual job enqueues.

**Architecture:** A new scheduler coroutine (`run_scheduler`) runs concurrently with the existing pull-only worker loop. Each tick, *if* automation is globally enabled, it enqueues two idempotent driver jobs. A new `refresh_due_accounts` handler scans for stale accounts (`Account.last_refreshed_at` older than a configurable interval) belonging to opted-in tenants (`Tenant.automation_enabled`), stamps them, and enqueues one `process_account` job each. An admin-gated API toggles the per-tenant flag. A global master switch (`automation_enabled`) defaults off so the offline test suite stays deterministic and zero-network.

**Tech Stack:** Python 3.10 (`from __future__ import annotations`), async SQLAlchemy 2.0, Pydantic v2 + pydantic-settings (`NEXUS_` prefix), FastAPI, Alembic, pytest (`asyncio_mode=auto`).

---

## Context the engineer needs before starting

**Run all commands from the worktree root** `.worktrees/continuous-automation` (created by the using-git-worktrees skill before execution). Test command base: `python -m pytest`.

**Key existing files (read these once):**
- `nexus/pipeline.py::process_account(ts, account)` — the SDR loop: ingest signals → score → inbox → plays → alerts. Already a worker handler via `handle_process_account`. We re-run it per stale account; we do **not** modify it.
- `nexus/workers/tasks.py` — `HANDLERS` dict, `enqueue_*` helpers, `dispatch`, `tenant_session` context manager, and the existing `handle_advance_cadences` + `enqueue_advance_cadences` (the model we copy). `enqueue_process_account(tenant_id, account_id, *, queue=None)` already exists.
- `nexus/workers/worker.py` — `run_worker(*, stop, poll_timeout)` pull loop (unchanged) and `_main`.
- `nexus/workers/queue.py` — `Job`, `TaskQueue`, `InMemoryTaskQueue`, `get_task_queue()`, `set_task_queue(q)`.
- `nexus/core/config.py` — `Settings` (pydantic-settings). The `cadence_*` block at lines 77-80 is the pattern to mirror. `get_settings()` is `@lru_cache`d; flip a setting in tests with `monkeypatch.setattr(get_settings(), "<field>", value)`.
- `nexus/models/identity.py` — `Tenant(IdMixin, TimestampMixin, Base)`. **`Tenant` is NOT `TenantScoped`** — it has no `tenant_id` column; its PK `id` *is* the tenant id. Load the current tenant with `ts.session.get(Tenant, ts.tenant_id)` (NOT `ts.get(...)`, which checks `obj.tenant_id`).
- `nexus/models/account.py` — `Account(IdMixin, TimestampMixin, TenantScoped, Base)`.
- `nexus/api/routers/workspace.py` — admin router, `prefix="/workspace"`, all endpoints gated by `require(Permission.manage_workspace)`. This is where the toggle lives. Mounted under the global `/api` prefix → full paths are `/api/workspace/...`.
- `nexus/api/schemas.py` — Pydantic request/response models.
- `tests/conftest.py` — `fresh_db` is an **`autouse=True`** fixture that drops + `create_all`s every table before each test, so tests do **NOT** request a `db`/schema fixture by name (there is no `db` fixture). `offline_services` (autouse) forces the demo ingestion source + stub LLM. `client` fixture (httpx ASGITransport, base_url `http://test`); `signup(client, *, slug, email, company)` POSTs `/api/auth/signup` and returns an **owner** token; `auth(token)` → headers dict. `POST /api/auth/login` (body `{"email","password"}`) returns a token for an invited member.
- `migrations/versions/0007_cadence.py` — latest migration; the new one chains off it (`down_revision = "0007_cadence"`). Uses `op.batch_alter_table(...)` for column adds (SQLite-safe).

**Offline test contract:** SQLite builds schema via `Base.metadata.create_all` (NOT `alembic upgrade head`), so new ORM columns appear in tests automatically. The Alembic migration is for Postgres production and is verified by inspection + a revision-chain assertion. Tests must be zero-network: driver/scheduler tests only assert that jobs are **enqueued**, never dispatched (so `process_account`'s sources/LLM never run).

**Deviation from spec (intentional, flag for Codex):** the spec (section 4.8) named the path `/api/tenant/automation`. This plan places the toggle in the existing `workspace` router at `/api/workspace/automation` instead, because that router already owns admin-gated tenant settings and the `manage_workspace` permission; a separate one-endpoint `/tenant` router would be over-engineering. Same RBAC, same behavior.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `nexus/core/config.py` | 4 automation knobs (master switch, tick interval, refresh interval, batch size) | Modify |
| `nexus/models/identity.py` | `Tenant.automation_enabled` per-tenant opt-in flag | Modify |
| `nexus/models/account.py` | `Account.last_refreshed_at` staleness timestamp | Modify |
| `migrations/versions/0008_continuous_automation.py` | Postgres DDL for the two new columns + index | Create |
| `nexus/workers/tasks.py` | `handle_refresh_due_accounts` + `enqueue_refresh_due_accounts` + HANDLERS entry | Modify |
| `nexus/workers/scheduler.py` | `run_scheduler` heartbeat + `_enqueue_due` tick | Create |
| `nexus/workers/worker.py` | run scheduler concurrently with the pull loop in `_main` | Modify |
| `nexus/api/schemas.py` | `AutomationSettingsIn` / `AutomationSettingsOut` | Modify |
| `nexus/api/routers/workspace.py` | `GET`/`PATCH /workspace/automation` | Modify |
| `tests/test_continuous_automation.py` | all tests for the above | Create |

---

## Task 1: Config knobs

**Files:**
- Modify: `nexus/core/config.py` (after line 80, the `cadence_max_duration_days` line)
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_continuous_automation.py`:

```python
"""Continuous Automation (sub-project D): heartbeat scheduler + account refresh driver."""
from __future__ import annotations

from nexus.core.config import get_settings


def test_automation_config_defaults():
    s = get_settings()
    assert s.automation_enabled is False           # master switch OFF by default
    assert s.automation_tick_interval_s == 60
    assert s.account_refresh_interval_s == 21600    # 6h
    assert s.account_refresh_batch_size == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py::test_automation_config_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'automation_enabled'`

- [ ] **Step 3: Add the config knobs**

In `nexus/core/config.py`, immediately after the line `cadence_max_duration_days: int = 30 ...` (line 80), add:

```python
    # Continuous Automation (sub-project D): autonomous heartbeat that drives the recurring
    # GTM loop (account refresh + cadence advance). OFF by default (safe opt-in, like
    # cadence_enabled) so the test suite stays deterministic and zero-network.
    automation_enabled: bool = False            # global master switch for the heartbeat
    automation_tick_interval_s: int = 60        # heartbeat period (seconds)
    account_refresh_interval_s: int = 21600     # staleness threshold before re-processing (6h)
    account_refresh_batch_size: int = 100       # max accounts claimed per tick across tenants
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py::test_automation_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_continuous_automation.py
git commit -m "feat(automation): config knobs for continuous automation heartbeat"
```

---

## Task 2: `Tenant.automation_enabled` per-tenant opt-in

**Files:**
- Modify: `nexus/models/identity.py:11-17` (the `Tenant` class)
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_continuous_automation.py` (add `import pytest` and the tenancy/db imports at the top of the file as you go):

```python
import pytest

from nexus.core.db import get_sessionmaker
from nexus.models.identity import Tenant


@pytest.mark.asyncio
async def test_tenant_automation_flag_defaults_false():
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme", slug="acme-auto")
        s.add(t)
        await s.flush()
        assert t.automation_enabled is False
```

> No schema fixture is requested: `tests/conftest.py` has an `autouse=True` `fresh_db` fixture that `create_all`s every table before each test. `pytest.ini`/`pyproject` sets `asyncio_mode=auto`, so `async def test_*` runs without an explicit `@pytest.mark.asyncio` — but the marker is harmless and kept for clarity.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py::test_tenant_automation_flag_defaults_false -v`
Expected: FAIL with `TypeError: 'automation_enabled' is an invalid keyword argument for Tenant` or `AttributeError`.

- [ ] **Step 3: Add the column**

In `nexus/models/identity.py`, the `Tenant` class currently ends at the `slug` column. Add a `Boolean` column. Update the import line and class:

```python
from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
```

```python
class Tenant(IdMixin, TimestampMixin, Base):
    """The isolation boundary — one customer organization."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    # Continuous Automation opt-in: when True (and the global automation_enabled master
    # switch is on), this tenant's accounts/cadences are driven autonomously by the heartbeat.
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py::test_tenant_automation_flag_defaults_false -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/models/identity.py tests/test_continuous_automation.py
git commit -m "feat(automation): Tenant.automation_enabled per-tenant opt-in flag"
```

---

## Task 3: `Account.last_refreshed_at` staleness timestamp

**Files:**
- Modify: `nexus/models/account.py:4` (imports) and the `Account` class (after line 26, the `source` column)
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_continuous_automation.py`:

```python
from nexus.models.account import Account


@pytest.mark.asyncio
async def test_account_last_refreshed_at_defaults_none():
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme3", slug="acme-auto3")
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Beta Corp", domain="beta.example")
        s.add(acct)
        await s.flush()
        assert acct.last_refreshed_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py::test_account_last_refreshed_at_defaults_none -v`
Expected: FAIL with `AttributeError: 'Account' object has no attribute 'last_refreshed_at'`.

- [ ] **Step 3: Add the column**

In `nexus/models/account.py`, change the import line (line 4) to add `DateTime`:

```python
from sqlalchemy import DateTime, JSON, Float, ForeignKey, Integer, String, UniqueConstraint
```

Add `datetime` to the typing imports at the top of the file (after `from __future__ import annotations`):

```python
from datetime import datetime
```

In the `Account` class, after the `source` column (line 26), add:

```python
    # Continuous Automation: when the autonomous heartbeat last re-processed this account.
    # NULL = never refreshed (always due). Stamped when the refresh driver claims the account.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py::test_account_last_refreshed_at_defaults_none -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/models/account.py tests/test_continuous_automation.py
git commit -m "feat(automation): Account.last_refreshed_at staleness timestamp"
```

---

## Task 4: Alembic migration 0008 (Postgres DDL)

**Files:**
- Create: `migrations/versions/0008_continuous_automation.py`
- Test: `tests/test_continuous_automation.py`

> The offline suite never runs this migration (SQLite uses `create_all`). This task locks the migration's revision chain so the Postgres deploy path stays correct.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_continuous_automation.py`:

```python
import importlib.util
from pathlib import Path


def test_migration_0008_chains_from_0007():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0008_continuous_automation.py"
    spec = importlib.util.spec_from_file_location("mig_0008", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0008_continuous_automation"
    assert mod.down_revision == "0007_cadence"
    assert hasattr(mod, "upgrade") and hasattr(mod, "downgrade")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py::test_migration_0008_chains_from_0007 -v`
Expected: FAIL with `FileNotFoundError` / spec is None.

- [ ] **Step 3: Create the migration**

Create `migrations/versions/0008_continuous_automation.py`:

```python
"""Continuous Automation: per-tenant opt-in flag + account staleness timestamp.

Revision ID: 0008_continuous_automation
Revises: 0007_cadence
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_continuous_automation"
down_revision = "0007_cadence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column(
                "automation_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_accounts_last_refreshed_at", "accounts", ["last_refreshed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_last_refreshed_at", table_name="accounts")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("last_refreshed_at")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("automation_enabled")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py::test_migration_0008_chains_from_0007 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0008_continuous_automation.py tests/test_continuous_automation.py
git commit -m "feat(automation): alembic 0008 — automation_enabled + last_refreshed_at columns"
```

---

## Task 5: Refresh-due-accounts driver handler

**Files:**
- Modify: `nexus/workers/tasks.py` (add handler + enqueue helper + HANDLERS entry)
- Test: `tests/test_continuous_automation.py`

This is the core of the feature. The handler scans globally for stale accounts in opted-in tenants, stamps them, and enqueues a `process_account` job per account.

- [ ] **Step 1: Write the failing test (selection + claim + enqueue)**

Append to `tests/test_continuous_automation.py`:

```python
from datetime import datetime, timedelta, timezone

from nexus.core.config import get_settings
from nexus.workers.queue import InMemoryTaskQueue, set_task_queue, get_task_queue
from nexus.workers.tasks import handle_refresh_due_accounts


async def _drain(queue) -> list:
    jobs = []
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        jobs.append(job)
    return jobs


@pytest.mark.asyncio
async def test_refresh_selects_only_stale_in_opted_in_tenants(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=7)   # older than 6h interval → due
    fresh = now - timedelta(hours=1)   # within interval → not due

    async with get_sessionmaker()() as s:
        t = Tenant(name="Opted", slug="opted", automation_enabled=True)
        s.add(t)
        await s.flush()
        never = Account(tenant_id=t.id, name="Never", domain="never.x", last_refreshed_at=None)
        old = Account(tenant_id=t.id, name="Old", domain="old.x", last_refreshed_at=stale)
        recent = Account(tenant_id=t.id, name="Recent", domain="recent.x", last_refreshed_at=fresh)
        s.add_all([never, old, recent])
        await s.commit()
        never_id, old_id, recent_id = never.id, old.id, recent.id

    res = await handle_refresh_due_accounts({"now_iso": now.isoformat()})

    assert res["accounts"] == 2
    jobs = await _drain(q)
    enqueued_ids = {j.payload["account_id"] for j in jobs}
    assert all(j.name == "process_account" for j in jobs)
    assert enqueued_ids == {never_id, old_id}      # recent excluded

    # claimed accounts were stamped to `now`
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, never_id)).last_refreshed_at == now
        assert (await s.get(Account, old_id)).last_refreshed_at == now
        assert (await s.get(Account, recent_id)).last_refreshed_at == fresh
    set_task_queue(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py::test_refresh_selects_only_stale_in_opted_in_tenants -v`
Expected: FAIL with `ImportError: cannot import name 'handle_refresh_due_accounts'`.

- [ ] **Step 3: Implement the handler**

In `nexus/workers/tasks.py`, add the handler after `handle_advance_cadences` (after line 128) and before the `HANDLERS` dict:

```python
async def handle_refresh_due_accounts(payload: dict) -> dict:
    """Periodic account-refresh driver. Scans globally for accounts that are due for a
    refresh (stale or never refreshed) and belong to a tenant that has opted in, stamps
    each as claimed, and enqueues a ``process_account`` job per account.

    ``process_account`` runs the full sense→act loop (ingest signals → score → inbox →
    plays → alerts), so this one driver delivers ingestion refresh, rescoring, play
    evaluation, and alerts. Inert unless the global ``automation_enabled`` switch is set.

    The global scan uses a raw, tenant-agnostic session (it reads only ids), preserving
    per-tenant isolation for the actual stamping work below. ``now`` is overridable via
    ``payload['now_iso']`` for deterministic tests."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_, select

    from nexus.core.config import get_settings
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    settings = get_settings()
    if not settings.automation_enabled:
        return {"skipped": "automation_disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    cutoff = now - timedelta(seconds=settings.account_refresh_interval_s)
    batch = settings.account_refresh_batch_size

    async with get_sessionmaker()() as session:
        stmt = (
            select(Account.tenant_id, Account.id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(
                Tenant.automation_enabled == True,  # noqa: E712
                or_(
                    Account.last_refreshed_at.is_(None),
                    Account.last_refreshed_at <= cutoff,
                ),
            )
            .order_by(Account.last_refreshed_at.asc().nulls_first())
            .limit(batch)
        )
        if settings.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True, of=Account)
        rows = (await session.execute(stmt)).all()

    # group selected account ids by tenant
    by_tenant: dict[str, list[str]] = {}
    for tenant_id, account_id in rows:
        by_tenant.setdefault(tenant_id, []).append(account_id)

    refreshed = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                account.last_refreshed_at = now  # claim — excludes it from the next tick
                await enqueue_process_account(tid, aid)
                refreshed += 1

    return {"tenants": len(by_tenant), "accounts": refreshed}
```

Add the enqueue helper after `enqueue_advance_cadences` (after line 171):

```python
async def enqueue_refresh_due_accounts(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="refresh_due_accounts", payload={}))
```

Register the handler in the `HANDLERS` dict (after the `"advance_cadences"` entry):

```python
HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "run_campaign": handle_run_campaign,
    "advance_cadences": handle_advance_cadences,
    "refresh_due_accounts": handle_refresh_due_accounts,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py::test_refresh_selects_only_stale_in_opted_in_tenants -v`
Expected: PASS

- [ ] **Step 5: Write the idempotency + global-switch + isolation tests**

Append to `tests/test_continuous_automation.py`:

```python
@pytest.mark.asyncio
async def test_refresh_is_idempotent_within_interval(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Idem", slug="idem", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", last_refreshed_at=None))
        await s.commit()

    first = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert first["accounts"] == 1
    await _drain(q)
    # same `now` → the just-claimed account is no longer due
    second = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert second["accounts"] == 0
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_refresh_skips_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Off", slug="off", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", last_refreshed_at=None))
        await s.commit()
    res = await handle_refresh_due_accounts({})
    assert res == {"skipped": "automation_disabled"}
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_refresh_is_tenant_isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        opted = Tenant(name="In", slug="in-iso", automation_enabled=True)
        not_opted = Tenant(name="Out", slug="out-iso", automation_enabled=False)
        s.add_all([opted, not_opted])
        await s.flush()
        a_in = Account(tenant_id=opted.id, name="In", domain="in.x", last_refreshed_at=None)
        a_out = Account(tenant_id=not_opted.id, name="Out", domain="out.x", last_refreshed_at=None)
        s.add_all([a_in, a_out])
        await s.commit()
        in_id, out_id = a_in.id, a_out.id

    res = await handle_refresh_due_accounts({"now_iso": now.isoformat()})
    assert res["accounts"] == 1
    jobs = await _drain(q)
    assert {j.payload["account_id"] for j in jobs} == {in_id}

    async with get_sessionmaker()() as s:
        assert (await s.get(Account, out_id)).last_refreshed_at is None  # untouched
    set_task_queue(None)
```

- [ ] **Step 6: Run the new tests**

Run: `python -m pytest tests/test_continuous_automation.py -k refresh -v`
Expected: 4 PASS (selection, idempotency, master-switch, isolation).

- [ ] **Step 7: Commit**

```bash
git add nexus/workers/tasks.py tests/test_continuous_automation.py
git commit -m "feat(automation): refresh_due_accounts driver — stale-account selection + claim"
```

---

## Task 6: Heartbeat scheduler

**Files:**
- Create: `nexus/workers/scheduler.py`
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_continuous_automation.py`:

```python
from nexus.workers.scheduler import _enqueue_due, run_scheduler


@pytest.mark.asyncio
async def test_enqueue_due_enqueues_both_drivers_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    assert count == 2
    jobs = await _drain(q)
    assert {j.name for j in jobs} == {"advance_cadences", "refresh_due_accounts"}


@pytest.mark.asyncio
async def test_enqueue_due_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    count = await _enqueue_due(q)
    assert count == 0
    assert await _drain(q) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py -k enqueue_due -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.workers.scheduler'`.

- [ ] **Step 3: Create the scheduler**

Create `nexus/workers/scheduler.py`:

```python
"""Continuous Automation heartbeat.

A periodic coroutine that runs alongside the pull-only worker loop. Each tick, while the
global ``automation_enabled`` switch is on, it enqueues the recurring driver jobs
(``advance_cadences`` + ``refresh_due_accounts``). Both drivers are idempotent and
self-filtering, so enqueuing them every tick is safe and needs no per-job bookkeeping.

Run as part of ``python -m nexus.workers.worker`` (see ``worker.py``).
"""
from __future__ import annotations

import asyncio
import logging

from nexus.core.config import get_settings
from nexus.workers.queue import TaskQueue, get_task_queue
from nexus.workers.tasks import enqueue_advance_cadences, enqueue_refresh_due_accounts

logger = logging.getLogger("nexus.workers.scheduler")


async def _enqueue_due(queue: TaskQueue) -> int:
    """Enqueue the recurring drivers if automation is enabled. Returns the number enqueued."""
    if not get_settings().automation_enabled:
        return 0
    await enqueue_advance_cadences(queue=queue)
    await enqueue_refresh_due_accounts(queue=queue)
    return 2


async def run_scheduler(
    *, stop: asyncio.Event | None = None, queue: TaskQueue | None = None
) -> None:
    """Heartbeat loop: enqueue drivers each tick until ``stop`` is set. Inert (loops but
    enqueues nothing) while ``automation_enabled`` is off."""
    stop = stop or asyncio.Event()
    queue = queue or get_task_queue()
    logger.info("scheduler started")
    while not stop.is_set():
        await _enqueue_due(queue)
        try:
            # stop.wait() with a timeout makes shutdown prompt (no fixed sleep to drain).
            await asyncio.wait_for(stop.wait(), timeout=get_settings().automation_tick_interval_s)
        except asyncio.TimeoutError:
            pass  # tick elapsed; loop again
    logger.info("scheduler stopping")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py -k enqueue_due -v`
Expected: 2 PASS

- [ ] **Step 5: Write the prompt-shutdown test**

Append to `tests/test_continuous_automation.py`:

```python
@pytest.mark.asyncio
async def test_run_scheduler_stops_promptly_when_event_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    stop = asyncio.Event()
    stop.set()  # already set → loop body never runs, returns immediately
    q = InMemoryTaskQueue()
    await asyncio.wait_for(run_scheduler(stop=stop, queue=q), timeout=1.0)
    assert await _drain(q) == []
```

Add `import asyncio` to the top of the test file if not already present.

- [ ] **Step 6: Run the test**

Run: `python -m pytest tests/test_continuous_automation.py::test_run_scheduler_stops_promptly_when_event_set -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add nexus/workers/scheduler.py tests/test_continuous_automation.py
git commit -m "feat(automation): heartbeat scheduler (run_scheduler) enqueuing recurring drivers"
```

---

## Task 7: Wire the scheduler into the worker process

**Files:**
- Modify: `nexus/workers/worker.py` (imports + `_main`)
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_continuous_automation.py`:

```python
from nexus.workers.worker import run_worker


@pytest.mark.asyncio
async def test_worker_and_scheduler_stop_on_shared_event(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    stop = asyncio.Event()
    stop.set()  # both coroutines should observe this and return
    await asyncio.wait_for(
        asyncio.gather(
            run_worker(stop=stop, poll_timeout=0.1),
            run_scheduler(stop=stop, queue=q),
        ),
        timeout=2.0,
    )
    set_task_queue(None)
```

- [ ] **Step 2: Run test to verify it fails (or passes trivially)**

Run: `python -m pytest tests/test_continuous_automation.py::test_worker_and_scheduler_stop_on_shared_event -v`
Expected: PASS already (both coroutines exist and honor `stop`). This test exists to lock the concurrent-shutdown contract that `_main` relies on. If it fails, the worker/scheduler stop handling is broken — fix that before proceeding.

- [ ] **Step 3: Wire the scheduler into `_main`**

In `nexus/workers/worker.py`, update the imports near the top to add the scheduler:

```python
from nexus.workers.scheduler import run_scheduler
```

Replace the `try/finally` body of `_main` (lines 42-45) so worker + scheduler run concurrently on the shared `stop` event:

```python
    try:
        await asyncio.gather(
            run_worker(stop=stop),
            run_scheduler(stop=stop),
        )
    finally:
        await dispose_db()
```

`run_worker` itself is unchanged — it stays a pure `dequeue → dispatch` loop.

- [ ] **Step 4: Run the test + import smoke**

Run: `python -m pytest tests/test_continuous_automation.py::test_worker_and_scheduler_stop_on_shared_event -v`
Expected: PASS

Run: `python -c "import nexus.workers.worker"`
Expected: no output, exit 0 (no import errors).

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/worker.py tests/test_continuous_automation.py
git commit -m "feat(automation): run heartbeat scheduler alongside the worker pull loop"
```

---

## Task 8: Per-tenant automation toggle API

**Files:**
- Modify: `nexus/api/schemas.py` (add two schemas near the other workspace schemas, after line 188)
- Modify: `nexus/api/routers/workspace.py` (imports + two endpoints)
- Test: `tests/test_continuous_automation.py`

- [ ] **Step 1: Write the failing test (admin GET/PATCH + isolation)**

Append to `tests/test_continuous_automation.py`:

```python
from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_automation_toggle_get_and_patch(client):
    token = await signup(client, slug="toggle", email="owner@toggle.x", company="Toggle")
    # default off
    r = await client.get("/api/workspace/automation", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"automation_enabled": False}
    # turn on
    r = await client.patch(
        "/api/workspace/automation",
        headers=auth(token),
        json={"automation_enabled": True},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"automation_enabled": True}
    # reads back on
    r = await client.get("/api/workspace/automation", headers=auth(token))
    assert r.json() == {"automation_enabled": True}


@pytest.mark.asyncio
async def test_automation_toggle_isolated_between_tenants(client):
    a = await signup(client, slug="ta", email="a@ta.x", company="TenantA")
    b = await signup(client, slug="tb", email="b@tb.x", company="TenantB")
    await client.patch(
        "/api/workspace/automation", headers=auth(a), json={"automation_enabled": True}
    )
    r = await client.get("/api/workspace/automation", headers=auth(b))
    assert r.json() == {"automation_enabled": False}  # B unaffected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_continuous_automation.py -k automation_toggle -v`
Expected: FAIL with 404 (route does not exist yet).

- [ ] **Step 3: Add the schemas**

In `nexus/api/schemas.py`, after `WorkspaceOut` (ends at line 188), add:

```python
class AutomationSettingsIn(BaseModel):
    automation_enabled: bool


class AutomationSettingsOut(BaseModel):
    automation_enabled: bool
```

- [ ] **Step 4: Add the endpoints**

In `nexus/api/routers/workspace.py`, extend the schema import block (lines 13-19) to include the two new schemas:

```python
from nexus.api.schemas import (
    AutomationSettingsIn,
    AutomationSettingsOut,
    MemberInviteRequest,
    MemberOut,
    MemberRoleUpdate,
    WorkspaceIn,
    WorkspaceOut,
)
```

Add `Tenant` to the identity import (line 23):

```python
from nexus.models.identity import Membership, Tenant, User, Workspace
```

Add the two endpoints after the workspaces section (after `rename_workspace`, line 61):

```python
# ---- automation (continuous-automation opt-in) ----
@router.get("/automation", response_model=AutomationSettingsOut)
async def get_automation(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> AutomationSettingsOut:
    # Tenant is the isolation boundary itself (not TenantScoped); load via the raw session.
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return AutomationSettingsOut(automation_enabled=bool(tenant.automation_enabled))


@router.patch("/automation", response_model=AutomationSettingsOut)
async def set_automation(
    body: AutomationSettingsIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_workspace)),
) -> AutomationSettingsOut:
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    tenant.automation_enabled = body.automation_enabled
    await ts.flush()
    return AutomationSettingsOut(automation_enabled=tenant.automation_enabled)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_continuous_automation.py -k automation_toggle -v`
Expected: 2 PASS

- [ ] **Step 6: Write the RBAC test (rep is forbidden)**

Append to `tests/test_continuous_automation.py`:

```python
@pytest.mark.asyncio
async def test_automation_toggle_forbidden_for_rep(client):
    owner = await signup(client, slug="rbac", email="owner@rbac.x", company="RbacCo")
    # invite a rep, then log in as that rep
    inv = await client.post(
        "/api/workspace/members",
        headers=auth(owner),
        json={
            "email": "rep@rbac.x",
            "full_name": "Rep",
            "password": "password123",
            "role": "rep",
        },
    )
    assert inv.status_code == 201, inv.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@rbac.x", "password": "password123"}
    )
    assert login.status_code == 200, login.text
    rep_token = login.json()["access_token"]

    r = await client.patch(
        "/api/workspace/automation",
        headers=auth(rep_token),
        json={"automation_enabled": True},
    )
    assert r.status_code == 403, r.text
```

> If `MemberInviteRequest` requires different fields, check `nexus/api/schemas.py` (lines ~191-198) and match them. The invite + login flow is how `tests/` provisions a non-owner principal.

- [ ] **Step 7: Run the RBAC test**

Run: `python -m pytest tests/test_continuous_automation.py::test_automation_toggle_forbidden_for_rep -v`
Expected: PASS (403)

- [ ] **Step 8: Commit**

```bash
git add nexus/api/schemas.py nexus/api/routers/workspace.py tests/test_continuous_automation.py
git commit -m "feat(automation): admin-gated per-tenant automation toggle API"
```

---

## Task 9: Full-suite green + module green

**Files:** none (verification only)

- [ ] **Step 1: Run the new module**

Run: `python -m pytest tests/test_continuous_automation.py -v`
Expected: all tests PASS (config, 2 model, migration, 4 refresh, 3 scheduler/worker, 3 API = ~13 tests).

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest`
Expected: previous 265 + the new continuous-automation tests, 0 failures. (The suite takes ~14 minutes; run in the background and check the result.)

- [ ] **Step 3: If anything failed, fix it and re-run.** Do not proceed with a red suite. Common gotchas:
  - Do NOT add a `db` parameter to tests — schema creation is the `autouse` `fresh_db` fixture; requesting a non-existent `db` fixture errors.
  - Leftover global queue between tests → ensure each queue-using test calls `set_task_queue(None)` at the end (already in the tests above).
  - `nulls_first()` unsupported on an old SQLite → confirm the runtime SQLite is ≥ 3.30 (it is in CI); the ORDER BY is still correct without it on SQLite because NULLs sort first for ASC there.

- [ ] **Step 4: Commit (only if any fixes were made)**

```bash
git add -p   # stage only the specific fixes
git commit -m "test(automation): full-suite green for continuous automation"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Spec §2 core insight (reuse `process_account`) → Task 5 enqueues `process_account`, no pipeline changes. ✓
- Spec §4.1 config knobs → Task 1. ✓
- Spec §4.2 `Tenant.automation_enabled` → Task 2. ✓
- Spec §4.3 `Account.last_refreshed_at` → Task 3. ✓
- Spec §4.4 migration 0008 → Task 4. ✓
- Spec §4.5 refresh driver (gating, raw scan, SKIP LOCKED on PG, group-by-tenant, claim, enqueue) → Task 5. ✓
- Spec §4.6 scheduler (`run_scheduler`, enqueue both, `stop.wait` timeout) → Task 6. ✓
- Spec §4.7 worker wiring (`asyncio.gather`, `run_worker` unchanged) → Task 7. ✓
- Spec §4.8 toggle API (admin-gated GET/PATCH) → Task 8 (path is `/api/workspace/automation`; documented deviation). ✓
- Spec §5 tests (gating, selection+claim, idempotency, isolation, global-switch, API+RBAC, full suite) → Tasks 1-9. ✓
- Spec §6 out-of-scope items → none implemented. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test shows full assertions. ✓

**Type/name consistency:** `handle_refresh_due_accounts` / `enqueue_refresh_due_accounts` / `"refresh_due_accounts"` job name used consistently across Tasks 5, 6. `run_scheduler` / `_enqueue_due` consistent across Tasks 6, 7. `AutomationSettingsIn`/`AutomationSettingsOut` consistent across Task 8. `automation_enabled` field name consistent everywhere (config, Tenant, schema, API). `last_refreshed_at` consistent across Tasks 3, 4, 5. ✓
