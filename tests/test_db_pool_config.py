"""Connection-pool sizing is configuration, not a constant.

A managed Postgres has a hard, SKU-dependent `max_connections`. Peak usage is
`replicas x processes x (pool + overflow)` — and it roughly DOUBLES during a rolling deploy,
because the old and new revisions serve simultaneously. Exceeding it does not degrade
gracefully: new connections are refused, which surfaces as 500s under exactly the load that
caused it.

These tests exist for two reasons:

  1. The values must be reachable from env, so a small instance can be tuned DOWN in config
     rather than by editing code or paying for a larger SKU.
  2. The DEFAULTS must not move. They are the values that were hardcoded before this became
     configurable, so any existing deployment that sets nothing must behave identically. A
     silent default change here would alter connection usage across every environment at once.
"""

from __future__ import annotations

import nexus.core.db as db_module
from nexus.core.config import Settings


def test_defaults_match_the_previously_hardcoded_values():
    """Regression guard: an existing deployment that sets nothing must not change behaviour."""
    s = Settings()
    assert s.db_pool_size == 10
    assert s.db_max_overflow == 20
    assert s.db_platform_pool_size == 2
    assert s.db_platform_max_overflow == 3


def test_pool_sizes_are_settable_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_DB_POOL_SIZE", "5")
    monkeypatch.setenv("NEXUS_DB_MAX_OVERFLOW", "5")
    monkeypatch.setenv("NEXUS_DB_PLATFORM_POOL_SIZE", "1")
    monkeypatch.setenv("NEXUS_DB_PLATFORM_MAX_OVERFLOW", "2")

    s = Settings()
    assert (s.db_pool_size, s.db_max_overflow) == (5, 5)
    assert (s.db_platform_pool_size, s.db_platform_max_overflow) == (1, 2)


def test_engine_applies_the_configured_pool_for_postgres(monkeypatch):
    """The settings must actually reach create_async_engine, not just exist on Settings."""
    captured: dict = {}

    def _fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    monkeypatch.setattr(db_module, "_engine", None)

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/nexus",
        db_pool_size=5,
        db_max_overflow=5,
    )
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)

    db_module.get_engine()

    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 5
    # pool_pre_ping stays on: a Postgres that recycled a connection underneath us must surface
    # as a retry, not as a request-time failure.
    assert captured["pool_pre_ping"] is True

    monkeypatch.setattr(db_module, "_engine", None)


def test_sqlite_ignores_pool_settings(monkeypatch):
    """SQLite has no server-side connection limit; passing pool_size to it is an error."""
    captured: dict = {}

    def _fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_install_sqlite_pragmas", lambda engine: None)

    settings = Settings(database_url="sqlite+aiosqlite:///./x.db", db_pool_size=5)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)

    db_module.get_engine()

    assert "pool_size" not in captured
    assert "max_overflow" not in captured

    monkeypatch.setattr(db_module, "_engine", None)
