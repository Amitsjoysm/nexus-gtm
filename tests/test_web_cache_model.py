"""The cache is platform-global, like `companies` and `people`.

A `tenant_id` here would be enrolled by `scripts/apply_rls.py`, and the shared reader would then
see zero rows under Postgres RLS — silently, because RLS misses are not errors. Every SQLite test
would still pass.
"""
from __future__ import annotations


def test_web_cache_is_platform_global():
    from nexus.models.web_cache import WebCache

    columns = set(WebCache.__table__.columns.keys())
    assert "tenant_id" not in columns
    assert {"id", "kind", "subject", "engine", "payload", "fetched_at", "expires_at",
            "hit_count", "bytes"} <= columns


def test_expiry_is_indexed_because_the_prune_sweep_scans_it():
    from nexus.models.web_cache import WebCache

    indexed = {tuple(c.name for c in ix.columns) for ix in WebCache.__table__.indexes}
    assert ("expires_at",) in indexed
