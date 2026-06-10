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
