"""Regression tests for production-hardening changes."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.core.config import Settings
from nexus.lists.builder import get_list_builder
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from tests.conftest import make_tenant, tenant_session


def test_prod_rejects_insecure_default_secret():
    with pytest.raises(ValidationError):
        Settings(env="prod", _env_file=None)


def test_prod_accepts_a_real_secret():
    s = Settings(env="prod", secret_key="x" * 48, _env_file=None)
    assert s.env == "prod"


def test_cors_origin_list_parsing():
    s = Settings(cors_origins="https://a.com, chrome-extension://abc", _env_file=None)
    assert s.cors_origin_list == ["https://a.com", "chrome-extension://abc"]


async def test_list_builder_filters_by_latest_composite():
    tid = await make_tenant()
    builder = get_list_builder()
    async with tenant_session(tid) as ts:
        hot = Account(tenant_id=tid, name="Hot", domain="hot.co", industry="Software")
        cold = Account(tenant_id=tid, name="Cold", domain="cold.co", industry="Software")
        ts.add_all([hot, cold])
        await ts.flush()
        ts.add_all([
            AccountScore(tenant_id=tid, account_id=hot.id, composite=85),
            AccountScore(tenant_id=tid, account_id=cold.id, composite=20),
        ])
        await ts.flush()

        matches = await builder.preview(ts, {"industries": ["Software"], "min_composite": 60})
        assert [a.name for a in matches] == ["Hot"]
