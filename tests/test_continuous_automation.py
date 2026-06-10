"""Continuous Automation (sub-project D): heartbeat scheduler + account refresh driver."""
from __future__ import annotations

import pytest

import importlib.util
from pathlib import Path

from nexus.core.config import get_settings
from nexus.core.db import get_sessionmaker
from nexus.models.identity import Tenant
from nexus.models.account import Account


def test_automation_config_defaults():
    s = get_settings()
    assert s.automation_enabled is False           # master switch OFF by default
    assert s.automation_tick_interval_s == 60
    assert s.account_refresh_interval_s == 21600    # 6h
    assert s.account_refresh_batch_size == 100


@pytest.mark.asyncio
async def test_tenant_automation_flag_defaults_false():
    async with get_sessionmaker()() as s:
        t = Tenant(name="Acme", slug="acme-auto")
        s.add(t)
        await s.flush()
        assert t.automation_enabled is False


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


def test_migration_0008_chains_from_0007():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0008_continuous_automation.py"
    spec = importlib.util.spec_from_file_location("mig_0008", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0008_continuous_automation"
    assert mod.down_revision == "0007_cadence"
    assert hasattr(mod, "upgrade") and hasattr(mod, "downgrade")
