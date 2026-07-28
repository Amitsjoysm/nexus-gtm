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


def test_usage_migration_creates_tables():
    import importlib
    import inspect

    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0022_billing_usage")
    assert mod.revision == "0022_billing_usage"
    assert mod.down_revision == "0021_billing_foundation"
    src = inspect.getsource(mod.upgrade)
    for table in ("billing_usage_events", "billing_usage_rollups"):
        assert table in Base.metadata.tables
        assert f'"{table}"' in src or f"'{table}'" in src


def test_money_migration_creates_tables():
    import importlib
    import inspect

    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0024_billing_money")
    assert mod.revision == "0024_billing_money"
    assert mod.down_revision == "0023_billing_rollup_marker"
    src = inspect.getsource(mod.upgrade)
    for t in ("billing_rate_cards", "billing_cost_rates", "billing_credit_ledger",
              "billing_invoices", "billing_invoice_lines"):
        assert t in Base.metadata.tables
        assert f'"{t}"' in src or f"'{t}'" in src
    down = inspect.getsource(mod.downgrade)
    assert down.index("billing_invoice_lines") < down.index("billing_invoices")
