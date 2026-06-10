"""CRM Auto-Sync (sub-project E): change-aware outbound sync via hybrid triggers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from nexus.core.config import get_settings


def test_crm_sync_config_defaults():
    s = get_settings()
    assert s.crm_sync_enabled is False        # master switch OFF by default
    assert s.crm_sync_batch_size == 100


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
