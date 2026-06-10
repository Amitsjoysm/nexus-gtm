"""Continuous Automation (sub-project D): heartbeat scheduler + account refresh driver."""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from nexus.core.db import get_sessionmaker
from nexus.models.identity import Tenant


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
