# tests/test_billing_migration.py
from __future__ import annotations


def test_migration_creates_every_billing_table():
    import importlib
    import inspect

    import nexus.models  # noqa: F401  (register mappers)
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0021_billing_foundation")
    assert mod.revision == "0021_billing_foundation"
    assert mod.down_revision == "0020_account_archived_at"

    src = inspect.getsource(mod.upgrade)
    for table in (
        "billing_capabilities", "billing_plans", "billing_plan_entitlements",
        "billing_subscriptions", "platform_admins",
    ):
        assert table in Base.metadata.tables, f"{table} missing from models"
        assert f'"{table}"' in src or f"'{table}'" in src, f"{table} not created by migration"

    # Downgrade must drop children before parents (FK-safe).
    down = inspect.getsource(mod.downgrade)
    assert down.index("billing_plan_entitlements") < down.index("billing_plans")
    assert down.index("billing_subscriptions") < down.index("billing_plans")
