# CRM Auto-Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously and automatically push account state (record + contacts + recent-signal activity) to the configured CRM, change-aware and off by default, via a hybrid trigger (the `account.scored` EventBus fast-path + the Continuous-Automation heartbeat sweep backstop).

**Architecture:** A new `Account.crm_synced_at` change-detection column drives due-selection (`crm_synced_at IS NULL OR updated_at > crm_synced_at`). One shared `sync_account_to_crm` push unit is invoked by two thin worker handlers — `handle_sync_crm_account` (event single-account) and `handle_sync_crm_due_accounts` (heartbeat global sweep). `process_account` publishes a generic `account.scored` event; a subscriber enqueues the single-account job. Gated by a global `crm_sync_enabled` switch AND the reused per-tenant `automation_enabled`. Zero-network in tests via the recording stub connector.

**Tech Stack:** Python 3.10 (`from __future__ import annotations`), async SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Pydantic v2 + pydantic-settings (`NEXUS_` prefix), FastAPI, Alembic, pytest (`asyncio_mode=auto`). Multi-tenant via `TenantSession` + Postgres RLS; RBAC reuses `Permission.manage_accounts`. Offline: SQLite + stub LLM + in-memory queue, zero network.

**Spec:** `docs/superpowers/specs/2026-06-10-crm-auto-sync-design.md`

---

## Reference: existing shapes this plan builds on

- `nexus/ingestion/crm.py` — `CRMConnector` (ABC) with `push_account(account, *, contacts=None) -> CRMPushResult` and `push_activity(*, account_id, kind, detail=None) -> CRMPushResult`; both **record** into `self.pushed_accounts` / `self.pushed_activities` buffers (zero-network) and never raise. `StubCRMConnector` (source `"stub"`). `get_crm_connector()` / `set_crm_connector(connector)` global. `build_crm_connector_from_settings()` resolves `crm_provider`.
- `nexus/core/db.py` — `TZDateTime` (tz-aware decorator), `ensure_aware(dt)`, `TimestampMixin` (`created_at`, `updated_at` with `onupdate=utcnow`), `get_sessionmaker()`.
- `nexus/core/events.py` — `Event(name, tenant_id, payload, ...)`, `EventBus.subscribe(name, handler)`, `get_event_bus()`. `publish` is a no-op when a name has no handlers.
- `nexus/pipeline.py::process_account(ts, account)` — scores + ingests signals; returns a result dict with `new_signals` and `composite_score`.
- `nexus/workers/tasks.py` — `tenant_session(tid)` context manager, `Handler` type, `Job`, `get_task_queue`, `TaskQueue`, `HANDLERS` dict, `enqueue_*` helpers, `handle_refresh_due_accounts` (the sweep template).
- `nexus/workers/scheduler.py` — `_enqueue_due(queue)` + `run_scheduler`.
- `nexus/core/tenancy.py::TenantSession` — `.session` (raw `AsyncSession`), `.get`, `.list`, `.select`, `.add`, `.tenant_id`.
- Test helpers (`tests/test_continuous_automation.py`, `tests/conftest.py`): `InMemoryTaskQueue`, `set_task_queue`, `get_task_queue`, the `_drain(queue)` pattern, `client` fixture, `signup(client, ...)`, `auth(token)`.

All new tests go in `tests/test_crm_auto_sync.py` (created in Task 4, appended thereafter).

---

## Task 1: Config knobs

**Files:**
- Modify: `nexus/core/config.py` (add after the `automation_*` block)
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_crm_auto_sync.py`:

```python
"""CRM Auto-Sync (sub-project E): change-aware outbound sync via hybrid triggers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from nexus.core.config import get_settings


def test_crm_sync_config_defaults():
    s = get_settings()
    assert s.crm_sync_enabled is False        # master switch OFF by default
    assert s.crm_sync_batch_size == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crm_auto_sync.py::test_crm_sync_config_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'crm_sync_enabled'`

- [ ] **Step 3: Add the knobs**

In `nexus/core/config.py`, locate the Continuous-Automation block (the `automation_enabled` / `account_refresh_batch_size` lines) and add immediately after it:

```python
    # CRM Auto-Sync (sub-project E): continuously push account state to the configured CRM.
    # OFF by default (safe opt-in, like automation_enabled) so the suite is deterministic and
    # zero-network (tests use the recording stub connector). Change-aware via Account.crm_synced_at,
    # so there is no interval knob — only stale/changed accounts are pushed.
    crm_sync_enabled: bool = False        # global master switch for auto-sync
    crm_sync_batch_size: int = 100        # max accounts claimed per heartbeat sweep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_crm_auto_sync.py::test_crm_sync_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add crm_sync_enabled + crm_sync_batch_size config knobs"
```

---

## Task 2: `Account.crm_synced_at` change-detection column

**Files:**
- Modify: `nexus/models/account.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crm_auto_sync.py`:

```python
import pytest

from nexus.core.db import get_sessionmaker
from nexus.models.account import Account
from nexus.models.identity import Tenant


@pytest.mark.asyncio
async def test_account_crm_synced_at_defaults_none():
    async with get_sessionmaker()() as s:
        t = Tenant(name="CrmCo", slug="crmco-default")
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Beta", domain="beta.crm")
        s.add(acct)
        await s.flush()
        assert acct.crm_synced_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crm_auto_sync.py::test_account_crm_synced_at_defaults_none -v`
Expected: FAIL with `TypeError: 'crm_synced_at' is an invalid keyword argument` or `AttributeError`

- [ ] **Step 3: Add the column**

In `nexus/models/account.py`, the imports already include `datetime` and `TZDateTime` is imported from `nexus.core.db` alongside `Base, IdMixin, TimestampMixin`. Add to the `Account` class, immediately after the existing `last_refreshed_at` column:

```python
    # CRM Auto-Sync: when this account's state was last pushed to the CRM. NULL = never synced
    # (always due). Stamped on a successful push; the account is due again only when updated_at
    # moves past it. Indexed for the NULLS-FIRST due-selection scan.
    crm_synced_at: Mapped[datetime | None] = mapped_column(
        TZDateTime(), nullable=True, index=True
    )
```

(`TZDateTime` is already imported in this module — it backs `last_refreshed_at`. If not, add it to the `from nexus.core.db import ...` line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_crm_auto_sync.py::test_account_crm_synced_at_defaults_none -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/models/account.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add Account.crm_synced_at change-detection column"
```

---

## Task 3: Alembic migration `0009`

**Files:**
- Create: `migrations/versions/0009_crm_auto_sync.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crm_auto_sync.py`:

```python
def test_migration_0009_chains_from_0008():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations" / "versions" / "0009_crm_auto_sync.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0009", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0009_crm_auto_sync"
    assert mod.down_revision == "0008_continuous_automation"
    assert hasattr(mod, "upgrade") and hasattr(mod, "downgrade")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crm_auto_sync.py::test_migration_0009_chains_from_0008 -v`
Expected: FAIL with `FileNotFoundError` (the migration file does not exist yet)

- [ ] **Step 3: Create the migration**

Create `migrations/versions/0009_crm_auto_sync.py`:

```python
"""CRM Auto-Sync: add accounts.crm_synced_at + index.

Revision ID: 0009_crm_auto_sync
Revises: 0008_continuous_automation
Create Date: 2026-06-10

Note: the offline test path builds schema via Base.metadata.create_all (not alembic upgrade
head), matching migrations 0005-0008. This migration is for Postgres production; it is verified
by the revision-chain assertion in tests/test_crm_auto_sync.py, not by running upgrade head on
SQLite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_crm_auto_sync"
down_revision = "0008_continuous_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("crm_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_accounts_crm_synced_at", "accounts", ["crm_synced_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_crm_synced_at", table_name="accounts")
    op.drop_column("accounts", "crm_synced_at")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_crm_auto_sync.py::test_migration_0009_chains_from_0008 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0009_crm_auto_sync.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add Alembic 0009 for accounts.crm_synced_at"
```

---

## Task 4: Shared push unit — `sync_account_to_crm`

**Files:**
- Create: `nexus/ingestion/crm_sync.py`
- Test: `tests/test_crm_auto_sync.py`

This is the single, change-aware "push one account" function both triggers reuse. It pushes the account record + contacts always; for an already-synced account it also pushes one activity per signal ingested since the last sync; then stamps `crm_synced_at`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crm_auto_sync.py`:

```python
from datetime import datetime, timedelta, timezone

from nexus.ingestion.crm import StubCRMConnector
from nexus.models.account import Contact
from nexus.models.signal import SignalEvent
from nexus.workers.tasks import tenant_session


@pytest.mark.asyncio
async def test_sync_account_pushes_record_and_contacts_and_stamps():
    from nexus.ingestion.crm_sync import sync_account_to_crm

    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Push", slug="push-rec", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Acme", domain="acme.x", crm_synced_at=None)
        s.add(acct)
        await s.flush()
        s.add(Contact(tenant_id=t.id, account_id=acct.id, full_name="Jo Lead"))
        await s.commit()
        tid, aid = t.id, acct.id

    conn = StubCRMConnector()
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        res = await sync_account_to_crm(ts, acct, connector=conn, now=now)

    assert res.ok is True
    assert len(conn.pushed_accounts) == 1
    assert conn.pushed_accounts[0]["account_id"] == aid
    assert len(conn.pushed_accounts[0]["contacts"]) == 1
    # never-synced account: NO historical activity backfill on first sync
    assert conn.pushed_activities == []
    # crm_synced_at stamped to `now`
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, aid)).crm_synced_at == now


@pytest.mark.asyncio
async def test_sync_account_pushes_activity_for_signals_since_last_sync():
    from nexus.ingestion.crm_sync import sync_account_to_crm

    prior = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Act", slug="act-sig", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Acme2", domain="acme2.x", crm_synced_at=prior)
        s.add(acct)
        await s.flush()
        # one OLD signal (before last sync) and one NEW signal (after) — only NEW becomes activity
        s.add(SignalEvent(
            tenant_id=t.id, account_id=acct.id, kind="funding", source="news",
            title="Old round", dedupe_key="old-1", created_at=prior - timedelta(hours=2),
        ))
        s.add(SignalEvent(
            tenant_id=t.id, account_id=acct.id, kind="news", source="news",
            title="Fresh news", dedupe_key="new-1", created_at=now - timedelta(minutes=5),
        ))
        await s.commit()
        tid, aid = t.id, acct.id

    conn = StubCRMConnector()
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        await sync_account_to_crm(ts, acct, connector=conn, now=now)

    assert len(conn.pushed_accounts) == 1
    assert len(conn.pushed_activities) == 1          # only the post-last-sync signal
    act = conn.pushed_activities[0]
    assert act["kind"] == "signal"
    assert act["detail"]["signal"] == "Fresh news"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crm_auto_sync.py -k sync_account -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.ingestion.crm_sync'`

- [ ] **Step 3: Create the module**

Create `nexus/ingestion/crm_sync.py`:

```python
"""CRM auto-sync: the shared "push one account" unit + the event fast-path subscriber.

`sync_account_to_crm` is the single source of truth for what an automatic CRM sync does; both
the heartbeat sweep (handle_sync_crm_due_accounts) and the event single-account job
(handle_sync_crm_account) call it. It is change-aware via Account.crm_synced_at and never raises
across the connector boundary (push_* return CRMPushResult).
"""
from __future__ import annotations

from datetime import datetime

from nexus.core.config import get_settings
from nexus.core.db import ensure_aware
from nexus.core.events import Event, EventBus, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import CRMConnector, CRMPushResult
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent


async def sync_account_to_crm(
    ts: TenantSession, account: Account, *, connector: CRMConnector, now: datetime
) -> CRMPushResult:
    """Push one account's state to the CRM and stamp crm_synced_at on success.

    Always pushes the account record + contact roster. For an already-synced account, also
    pushes one activity per signal ingested since the last sync (created_at > crm_synced_at).
    A never-synced account gets the record + contacts but NO historical activity backfill
    (anti-flood). Stamps crm_synced_at = now only when push_account succeeds, so a failed push
    leaves the account due for retry on the next sweep (self-healing).
    """
    contacts = await ts.list(Contact, Contact.account_id == account.id)
    res = await connector.push_account(account, contacts=contacts)
    if not res.ok:
        return res

    prior = account.crm_synced_at
    if prior is not None:  # no first-sync activity backfill
        prior = ensure_aware(prior)
        signals = await ts.list(SignalEvent, SignalEvent.account_id == account.id)
        for sig in signals:
            if ensure_aware(sig.created_at) > prior:
                await connector.push_activity(
                    account_id=account.crm_id or account.id,
                    kind="signal",
                    detail={"signal": sig.title, "kind": sig.kind, "source": sig.source},
                )
    account.crm_synced_at = now
    return res


async def on_account_scored(event: Event) -> None:
    """Event fast-path: enqueue a single-account CRM sync when auto-sync is enabled.

    The authoritative gating (global switch + per-tenant opt-in) lives in the handler; this is
    a cheap pre-filter so we do not enqueue no-op jobs when auto-sync is entirely off.
    """
    if not get_settings().crm_sync_enabled:
        return
    # Lazy import avoids a tasks <-> crm_sync import cycle at module load.
    from nexus.workers.tasks import enqueue_sync_crm_account

    await enqueue_sync_crm_account(event.tenant_id, event.payload["account_id"])


def register_crm_sync_subscribers(bus: EventBus | None = None) -> None:
    """Subscribe the CRM-sync fast-path to ``account.scored``. Called from app + worker startup."""
    bus = bus or get_event_bus()
    bus.subscribe("account.scored", on_account_scored)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_crm_auto_sync.py -k sync_account -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add nexus/ingestion/crm_sync.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add change-aware sync_account_to_crm push unit"
```

---

## Task 5: Worker handlers + enqueue helpers + HANDLERS

**Files:**
- Modify: `nexus/workers/tasks.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crm_auto_sync.py`:

```python
from nexus.ingestion.crm import set_crm_connector
from nexus.workers.queue import InMemoryTaskQueue, set_task_queue
from nexus.workers.tasks import (
    handle_sync_crm_account,
    handle_sync_crm_due_accounts,
)


async def _drain(queue) -> list:
    jobs = []
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        jobs.append(job)
    return jobs


@pytest.mark.asyncio
async def test_sweep_skips_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    res = await handle_sync_crm_due_accounts({})
    assert res == {"skipped": "crm_sync_disabled"}


@pytest.mark.asyncio
async def test_sweep_selects_only_due_accounts_in_opted_in_tenants(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    t0 = now - timedelta(hours=3)
    t1 = now - timedelta(hours=1)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Sweep", slug="sweep-due", automation_enabled=True)
        s.add(t)
        await s.flush()
        never = Account(tenant_id=t.id, name="Never", domain="n.x", crm_synced_at=None)
        changed = Account(tenant_id=t.id, name="Changed", domain="c.x",
                          crm_synced_at=t0, updated_at=t1)        # updated after sync → due
        fresh = Account(tenant_id=t.id, name="Fresh", domain="f.x",
                        crm_synced_at=t1, updated_at=t0)          # synced after update → not due
        s.add_all([never, changed, fresh])
        await s.commit()
        never_id, changed_id, fresh_id = never.id, changed.id, fresh.id

    res = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})

    assert res["accounts"] == 2
    pushed_ids = {r["account_id"] for r in conn.pushed_accounts}
    assert pushed_ids == {never_id, changed_id}      # fresh excluded
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, never_id)).crm_synced_at == now
        assert (await s.get(Account, changed_id)).crm_synced_at == now
        assert (await s.get(Account, fresh_id)).crm_synced_at == t1   # untouched
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_sweep_is_idempotent(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="Idem", slug="sweep-idem", automation_enabled=True)
        s.add(t)
        await s.flush()
        s.add(Account(tenant_id=t.id, name="A", domain="a.x", crm_synced_at=None))
        await s.commit()

    first = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert first["accounts"] == 1
    second = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert second["accounts"] == 0                  # already stamped → no longer due
    assert len(conn.pushed_accounts) == 1
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_sweep_is_tenant_isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        opted = Tenant(name="In", slug="sweep-in", automation_enabled=True)
        out = Tenant(name="Out", slug="sweep-out", automation_enabled=False)
        s.add_all([opted, out])
        await s.flush()
        a_in = Account(tenant_id=opted.id, name="In", domain="in.x", crm_synced_at=None)
        a_out = Account(tenant_id=out.id, name="Out", domain="out.x", crm_synced_at=None)
        s.add_all([a_in, a_out])
        await s.commit()
        in_id, out_id = a_in.id, a_out.id

    res = await handle_sync_crm_due_accounts({"now_iso": now.isoformat()})
    assert res["accounts"] == 1
    assert {r["account_id"] for r in conn.pushed_accounts} == {in_id}
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, out_id)).crm_synced_at is None  # untouched
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_single_account_handler_event_path(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    now_present = True  # handler uses real now; we only assert the push happened + stamp set
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="One", slug="one-acct", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Solo", domain="solo.x", crm_synced_at=None)
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    res = await handle_sync_crm_account({"tenant_id": tid, "account_id": aid})
    assert res == {"account_id": aid, "ok": True}
    assert {r["account_id"] for r in conn.pushed_accounts} == {aid}
    async with get_sessionmaker()() as s:
        assert (await s.get(Account, aid)).crm_synced_at is not None
    set_crm_connector(None)


@pytest.mark.asyncio
async def test_single_account_handler_respects_tenant_opt_out(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    conn = StubCRMConnector()
    set_crm_connector(conn)
    async with get_sessionmaker()() as s:
        t = Tenant(name="OptOut", slug="opt-out", automation_enabled=False)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="NoSync", domain="ns.x", crm_synced_at=None)
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    res = await handle_sync_crm_account({"tenant_id": tid, "account_id": aid})
    assert res == {"skipped": "tenant_opted_out"}
    assert conn.pushed_accounts == []
    set_crm_connector(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crm_auto_sync.py -k "sweep or single_account" -v`
Expected: FAIL with `ImportError: cannot import name 'handle_sync_crm_account'`

- [ ] **Step 3: Add the handlers + enqueue helpers + HANDLERS entries**

In `nexus/workers/tasks.py`, add the two handlers (place them after `handle_refresh_due_accounts`):

```python
async def handle_sync_crm_due_accounts(payload: dict) -> dict:
    """Heartbeat backstop: scan globally for accounts that are due for a CRM sync (never synced
    or changed since last sync) in opted-in tenants, and push each to the configured CRM.

    Mirrors handle_refresh_due_accounts: a raw, tenant-agnostic id-scan (reads only ids, so
    per-tenant isolation is preserved by the RLS-scoped work below), then per-tenant sessions do
    the actual push via the shared sync_account_to_crm. Inert unless the global crm_sync_enabled
    switch is set. ``now`` is overridable via payload['now_iso'] for deterministic tests.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_, select

    from nexus.core.config import get_settings
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_sync import sync_account_to_crm
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    settings = get_settings()
    if not settings.crm_sync_enabled:
        return {"skipped": "crm_sync_disabled"}

    now = (
        datetime.fromisoformat(payload["now_iso"])
        if payload.get("now_iso")
        else datetime.now(timezone.utc)
    )
    batch = settings.crm_sync_batch_size

    async with get_sessionmaker()() as session:
        stmt = (
            select(Account.tenant_id, Account.id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(
                Tenant.automation_enabled == True,  # noqa: E712
                or_(
                    Account.crm_synced_at.is_(None),
                    Account.updated_at > Account.crm_synced_at,
                ),
            )
            .order_by(Account.crm_synced_at.asc().nulls_first())
            .limit(batch)
        )
        if settings.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True, of=Account)
        rows = (await session.execute(stmt)).all()

    by_tenant: dict[str, list[str]] = {}
    for tenant_id, account_id in rows:
        by_tenant.setdefault(tenant_id, []).append(account_id)

    connector = get_crm_connector()
    synced = 0
    for tid, account_ids in by_tenant.items():
        async with tenant_session(tid) as ts:
            for aid in account_ids:
                account = await ts.get(Account, aid)
                if account is None:
                    continue
                await sync_account_to_crm(ts, account, connector=connector, now=now)
                synced += 1

    return {"tenants": len(by_tenant), "accounts": synced}


async def handle_sync_crm_account(payload: dict) -> dict:
    """Event fast-path: sync a single account to the CRM. Gated by the global switch AND the
    tenant's automation_enabled opt-in (the authoritative gate)."""
    from datetime import datetime, timezone

    from nexus.core.config import get_settings
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_sync import sync_account_to_crm
    from nexus.models.account import Account
    from nexus.models.identity import Tenant

    if not get_settings().crm_sync_enabled:
        return {"skipped": "crm_sync_disabled"}

    tid = payload["tenant_id"]
    aid = payload["account_id"]
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        if tenant is None or not tenant.automation_enabled:
            return {"skipped": "tenant_opted_out"}
        account = await ts.get(Account, aid)
        if account is None:
            return {"skipped": "account_missing"}
        res = await sync_account_to_crm(
            ts, account, connector=get_crm_connector(), now=datetime.now(timezone.utc)
        )
    return {"account_id": aid, "ok": res.ok}
```

Add the enqueue helpers (place alongside the other `enqueue_*` helpers):

```python
async def enqueue_sync_crm_account(
    tenant_id: str, account_id: str, *, queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(name="sync_crm_account", payload={"tenant_id": tenant_id, "account_id": account_id})
    )


async def enqueue_sync_crm_due_accounts(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_crm_due_accounts", payload={}))
```

Add the two entries to the `HANDLERS` dict:

```python
    "sync_crm_account": handle_sync_crm_account,
    "sync_crm_due_accounts": handle_sync_crm_due_accounts,
```

Note: `Tenant` is fetched via `ts.session.get` (the raw session) because `Tenant` is a global, non-`TenantScoped` table — `ts.get` would reject it (it has no `tenant_id` attribute to match).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_crm_auto_sync.py -k "sweep or single_account" -v`
Expected: PASS (all 6)

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add sweep + single-account sync handlers and enqueue helpers"
```

---

## Task 6: `account.scored` event + subscriber wiring

**Files:**
- Modify: `nexus/pipeline.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crm_auto_sync.py`:

```python
from nexus.core.events import EventBus
from nexus.ingestion.crm_sync import on_account_scored, register_crm_sync_subscribers
from nexus.core.events import Event


@pytest.mark.asyncio
async def test_on_account_scored_enqueues_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    await on_account_scored(
        Event(name="account.scored", tenant_id="t1", payload={"account_id": "a1"})
    )
    jobs = await _drain(q)
    assert len(jobs) == 1
    assert jobs[0].name == "sync_crm_account"
    assert jobs[0].payload == {"tenant_id": "t1", "account_id": "a1"}
    set_task_queue(None)


@pytest.mark.asyncio
async def test_on_account_scored_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    await on_account_scored(
        Event(name="account.scored", tenant_id="t1", payload={"account_id": "a1"})
    )
    assert await _drain(q) == []
    set_task_queue(None)


@pytest.mark.asyncio
async def test_process_account_publishes_account_scored(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    set_task_queue(q)
    bus = EventBus()                      # isolated bus for this test
    register_crm_sync_subscribers(bus)
    monkeypatch.setattr("nexus.pipeline.get_event_bus", lambda: bus)

    async with get_sessionmaker()() as s:
        t = Tenant(name="Pub", slug="pub-evt", automation_enabled=True)
        s.add(t)
        await s.flush()
        acct = Account(tenant_id=t.id, name="Pub", domain="pub.x")
        s.add(acct)
        await s.commit()
        tid, aid = t.id, acct.id

    from nexus.pipeline import process_account

    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        await process_account(ts, acct)

    jobs = await _drain(q)
    assert any(j.name == "sync_crm_account" and j.payload["account_id"] == aid for j in jobs)
    set_task_queue(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crm_auto_sync.py -k "account_scored or publishes" -v`
Expected: `test_on_account_scored_*` PASS already (Task 4 added the function); `test_process_account_publishes_account_scored` FAILS (no `account.scored` published yet → no `sync_crm_account` job).

- [ ] **Step 3: Publish the event in `process_account`**

In `nexus/pipeline.py`, add the import at the top (with the other imports):

```python
from nexus.core.events import Event, get_event_bus
```

Then, at the end of `process_account`, immediately before the `return {...}` statement, publish the generic domain event:

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

    return {
        "account_id": account.id,
        ...
    }
```

(Keep the existing `return` dict unchanged; only insert the `publish` call before it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_crm_auto_sync.py -k "account_scored or publishes" -v`
Expected: PASS (all 3)

- [ ] **Step 5: Commit**

```bash
git add nexus/pipeline.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): publish account.scored from process_account for the sync fast-path"
```

---

## Task 7: Scheduler wiring (heartbeat enqueues the sweep)

**Files:**
- Modify: `nexus/workers/scheduler.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crm_auto_sync.py`:

```python
from nexus.workers.scheduler import _enqueue_due


@pytest.mark.asyncio
async def test_scheduler_enqueues_crm_sweep_when_crm_sync_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    q = InMemoryTaskQueue()
    await _enqueue_due(q)
    jobs = await _drain(q)
    assert {j.name for j in jobs} == {"sync_crm_due_accounts"}


@pytest.mark.asyncio
async def test_scheduler_omits_crm_sweep_when_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", False)
    q = InMemoryTaskQueue()
    await _enqueue_due(q)
    jobs = await _drain(q)
    assert "sync_crm_due_accounts" not in {j.name for j in jobs}
    assert {j.name for j in jobs} == {"advance_cadences", "refresh_due_accounts"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crm_auto_sync.py -k scheduler -v`
Expected: `test_scheduler_enqueues_crm_sweep_when_crm_sync_enabled` FAILS (no `sync_crm_due_accounts` enqueued).

- [ ] **Step 3: Wire the sweep into `_enqueue_due`**

In `nexus/workers/scheduler.py`, update the import line and `_enqueue_due`:

```python
from nexus.workers.tasks import (
    enqueue_advance_cadences,
    enqueue_refresh_due_accounts,
    enqueue_sync_crm_due_accounts,
)
```

```python
async def _enqueue_due(queue: TaskQueue) -> int:
    """Enqueue the recurring drivers for whichever switches are on. Returns the count enqueued.

    The cadence + account-refresh drivers gate on automation_enabled; the CRM sweep gates on its
    own crm_sync_enabled switch (so it can run independently). Each handler re-checks its switch,
    so this is a pre-filter, not the authority."""
    settings = get_settings()
    count = 0
    if settings.automation_enabled:
        await enqueue_advance_cadences(queue=queue)
        await enqueue_refresh_due_accounts(queue=queue)
        count += 2
    if settings.crm_sync_enabled:
        await enqueue_sync_crm_due_accounts(queue=queue)
        count += 1
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_crm_auto_sync.py -k scheduler -v`
Expected: PASS (both)

- [ ] **Step 5: Run the D scheduler tests to confirm no regression**

Run: `pytest tests/test_continuous_automation.py -k enqueue_due -v`
Expected: PASS — `test_enqueue_due_enqueues_both_drivers_when_enabled` still sees exactly `{advance_cadences, refresh_due_accounts}` (crm_sync_enabled defaults False), and the count is 2.

- [ ] **Step 6: Commit**

```bash
git add nexus/workers/scheduler.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): enqueue CRM sweep from the heartbeat, gated by crm_sync_enabled"
```

---

## Task 8: Subscriber registration in both entrypoints

**Files:**
- Modify: `nexus/main.py` (lifespan)
- Modify: `nexus/workers/worker.py` (`_main`)
- Test: `tests/test_crm_auto_sync.py`

The `EventBus` is a per-process singleton; `account.scored` is published in both the API process (synchronous `process_account`) and the worker process (the job). Register the subscriber in both.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crm_auto_sync.py`:

```python
@pytest.mark.asyncio
async def test_app_lifespan_registers_account_scored_subscriber():
    from nexus.core.events import get_event_bus
    from nexus.main import create_app

    bus = get_event_bus()
    before = len(bus._handlers.get("account.scored", []))
    app = create_app()
    async with app.router.lifespan_context(app):
        after = len(bus._handlers.get("account.scored", []))
        assert after >= before + 1   # the CRM-sync subscriber is registered on startup
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_crm_auto_sync.py::test_app_lifespan_registers_account_scored_subscriber -v`
Expected: FAIL (`after == before`; no subscriber registered).

- [ ] **Step 3: Register in the FastAPI lifespan**

In `nexus/main.py`, update the `lifespan`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from nexus.ingestion.crm_sync import register_crm_sync_subscribers

    register_crm_sync_subscribers()
    yield
    await dispose_db()
```

- [ ] **Step 4: Register in the worker `_main`**

In `nexus/workers/worker.py`, update `_main` to register before the `gather`:

```python
async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    from nexus.ingestion.crm_sync import register_crm_sync_subscribers

    register_crm_sync_subscribers()
    stop = asyncio.Event()
    ...
```

(Leave the rest of `_main` unchanged — the signal handlers and the `asyncio.gather(run_worker, run_scheduler)`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_crm_auto_sync.py::test_app_lifespan_registers_account_scored_subscriber -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nexus/main.py nexus/workers/worker.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): register account.scored subscriber in app + worker startup"
```

---

## Task 9: `GET /crm/sync-status` observability endpoint

**Files:**
- Modify: `nexus/api/schemas.py` (add `CRMSyncStatusOut`)
- Modify: `nexus/api/routers/integrations.py`
- Test: `tests/test_crm_auto_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crm_auto_sync.py`:

```python
from tests.conftest import auth, signup


@pytest.mark.asyncio
async def test_crm_sync_status_reports_flags_and_pending(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_sync_enabled", True)
    token = await signup(client, slug="cs", email="owner@cs.x", company="CsCo")
    # opt the tenant into automation so `enabled` composes True
    await client.patch(
        "/api/workspace/automation", headers=auth(token), json={"automation_enabled": True}
    )
    # create two accounts via inbound CRM sync (they land with crm_synced_at = NULL → pending)
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=auth(token),
        json={
            "source": "salesforce",
            "accounts": [
                {"external_id": "x1", "name": "One"},
                {"external_id": "x2", "name": "Two"},
            ],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/integrations/crm/sync-status", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["provider"] == "stub"
    assert body["pending"] == 2
    assert body["synced"] == 0


@pytest.mark.asyncio
async def test_crm_sync_status_forbidden_for_rep(client):
    owner = await signup(client, slug="csr", email="owner@csr.x", company="CsrCo")
    inv = await client.post(
        "/api/workspace/members",
        headers=auth(owner),
        json={"email": "rep@csr.x", "full_name": "Rep", "password": "password123", "role": "rep"},
    )
    assert inv.status_code == 201, inv.text
    login = await client.post(
        "/api/auth/login", json={"email": "rep@csr.x", "password": "password123"}
    )
    rep_token = login.json()["access_token"]
    r = await client.get("/api/integrations/crm/sync-status", headers=auth(rep_token))
    assert r.status_code == 403, r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crm_auto_sync.py -k sync_status -v`
Expected: FAIL with 404 (route does not exist).

- [ ] **Step 3: Add the response schema**

In `nexus/api/schemas.py`, add near the other CRM schemas (`CRMPushResponse`, etc.):

```python
class CRMSyncStatusOut(BaseModel):
    enabled: bool      # crm_sync_enabled (global) AND this tenant's automation_enabled
    provider: str      # configured crm_provider (stub|salesforce|hubspot)
    pending: int       # accounts due for sync (never synced or changed since last sync)
    synced: int        # accounts already up to date
```

(Use the module's existing Pydantic base — if other schemas subclass a shared base instead of `BaseModel`, match that.)

- [ ] **Step 4: Add the endpoint**

In `nexus/api/routers/integrations.py`, extend the imports:

```python
from sqlalchemy import func, or_, select

from nexus.api.schemas import (
    CRMPushResponse,
    CRMSyncRequest,
    CRMSyncResponse,
    CRMSyncStatusOut,
    SEPPushRequest,
    SEPPushResponse,
)
from nexus.core.config import get_settings
from nexus.models.identity import Tenant
```

Add the route (after `crm_push`):

```python
@router.get("/crm/sync-status", response_model=CRMSyncStatusOut)
async def crm_sync_status(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> CRMSyncStatusOut:
    """Auto-sync state for the current tenant: whether it is active, the provider, and how many
    accounts are pending vs. up to date. Counts are tenant-scoped (RLS) — never a global scan."""
    settings = get_settings()
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    enabled = bool(settings.crm_sync_enabled and tenant and tenant.automation_enabled)

    due_where = or_(
        Account.crm_synced_at.is_(None),
        Account.updated_at > Account.crm_synced_at,
    )
    total = await ts.session.scalar(
        select(func.count()).select_from(Account).where(Account.tenant_id == ts.tenant_id)
    )
    pending = await ts.session.scalar(
        select(func.count())
        .select_from(Account)
        .where(Account.tenant_id == ts.tenant_id, due_where)
    )
    total = total or 0
    pending = pending or 0
    return CRMSyncStatusOut(
        enabled=enabled,
        provider=(settings.crm_provider or "stub"),
        pending=pending,
        synced=total - pending,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_crm_auto_sync.py -k sync_status -v`
Expected: PASS (both)

- [ ] **Step 6: Commit**

```bash
git add nexus/api/schemas.py nexus/api/routers/integrations.py tests/test_crm_auto_sync.py
git commit -m "feat(crm-sync): add GET /crm/sync-status (tenant-scoped counts, manage_accounts)"
```

---

## Task 10: Full-suite green + regression sweep

**Files:**
- No new source; verification only.

- [ ] **Step 1: Run the new module in full**

Run: `pytest tests/test_crm_auto_sync.py -v`
Expected: PASS (all tests in the module).

- [ ] **Step 2: Run the adjacent subsystems that share touched files**

Run: `pytest tests/test_continuous_automation.py tests/test_api.py -v`
Expected: PASS. Confirms the scheduler change, the `process_account` event publish, the lifespan change, and the integrations router change did not regress D, the worker/scheduler, or the API.

- [ ] **Step 3: Run the entire suite**

Run: `pytest -q`
Expected: PASS — the prior 280 + the new CRM-auto-sync tests, 0 failures. (This run is slow, ~13 min; let it finish.)

- [ ] **Step 4: Final commit (only if Step 3 surfaced a fix)**

If any cross-cutting fix was required to get the suite green, commit it:

```bash
git add <fixed files>
git commit -m "test(crm-sync): keep full suite green after CRM auto-sync"
```

If nothing needed fixing, no commit — the feature is complete.

---

## Self-Review (run after writing; fixes folded in above)

- **Spec coverage:** §4.1 config → Task 1; §4.3 model → Task 2; §4.4 migration → Task 3; §5.4 push unit → Task 4; §5.5 event + §5.6 subscriber → Task 4 (subscriber) + Task 6 (publish); §5.7 handlers/enqueue → Task 5; §5.8 scheduler → Task 7; §5.9 registration → Task 8; §5.10 gating → enforced in Tasks 5 (handlers) + 7 (scheduler); §5.11 status endpoint → Task 9; §6 test plan → distributed across Tasks 1–9, full green in Task 10. ✓
- **Type/name consistency:** `sync_account_to_crm`, `on_account_scored`, `register_crm_sync_subscribers` (crm_sync.py); `handle_sync_crm_account`, `handle_sync_crm_due_accounts`, `enqueue_sync_crm_account`, `enqueue_sync_crm_due_accounts`, HANDLERS keys `sync_crm_account` / `sync_crm_due_accounts` (tasks.py); `account.scored` event name (pipeline + subscriber); `CRMSyncStatusOut` (schemas + router). All consistent across tasks. ✓
- **Dialect safety:** due-selection is column-vs-column in SQL (`Account.updated_at > Account.crm_synced_at`); activity filter is Python-side via `ensure_aware`. ✓
- **Gating authority:** both handlers re-check `crm_sync_enabled`; the single-account handler additionally re-checks `tenant.automation_enabled`; the sweep filters tenants on `automation_enabled` in SQL. Scheduler is a pre-filter only. ✓
- **No placeholders:** every code/test step contains complete code. ✓
