from __future__ import annotations


def test_migration_columns_match_models():
    """The migration must create every network table the ORM models declare (prod parity)."""
    import importlib

    import nexus.models  # noqa: F401  (register mappers)
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0018_relationship_graph")
    assert mod.down_revision == "0017_password_reset"
    assert mod.revision == "0018_relationship_graph"

    import inspect

    src = inspect.getsource(mod.upgrade)
    for table in (
        "network_source_accounts", "network_persons", "network_identities", "network_edges",
    ):
        assert table in Base.metadata.tables
        assert f'"{table}"' in src or f"'{table}'" in src
