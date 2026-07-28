from __future__ import annotations

# This file used to assert that `0018_relationship_graph` chained from `0017_password_reset` and
# named the four network tables in its source. Revisions 0001-0020 are now squashed into the
# frozen `0020_baseline_schema`, so that revision no longer exists as a separate file.
#
# tests/test_migrations_replay.py replaces it with a stronger guarantee — the whole chain is
# replayed onto an empty database and the result must match Base.metadata table-for-table and
# column-for-column, which covers the network tables along with everything else. What remains
# here is the part that check could not make: that the models themselves still declare them.


def test_network_tables_are_declared_by_the_models():
    import nexus.models  # noqa: F401  (register mappers)
    from nexus.core.db import Base

    for table in (
        "network_source_accounts", "network_persons", "network_identities", "network_edges",
    ):
        assert table in Base.metadata.tables
