"""`fresh_db` empties the tables between tests instead of rebuilding them (2026-10-13).

Rebuilding all ~90 tables before every test made the suite cross Azure's 45-minute job limit on a
2-vCPU agent. These pin what the cheaper reset must still guarantee. The tests depend on running in
file order on one worker, which `--dist loadfile` gives.
"""
from __future__ import annotations

from sqlalchemy import text


async def _tenants() -> int:
    from nexus.core.db import get_engine

    async with get_engine().connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM tenants"))).scalar()


async def test_1_writes_a_row():
    from tests.conftest import make_tenant

    await make_tenant(slug="fdb1")
    assert await _tenants() == 1


async def test_2_the_next_test_starts_empty():
    assert await _tenants() == 0


async def test_3_drops_a_table():
    from nexus.core.db import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text("DROP TABLE feature_switches"))


async def test_4_a_changed_schema_is_rebuilt():
    from nexus.core.db import get_engine

    async with get_engine().connect() as conn:
        rows = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='feature_switches'"
        ))).all()
    assert rows, "a table a test dropped must be back for the next test"
