"""Integration tests against REAL Postgres + Redis (not SQLite / in-memory).

These verify the two production-only code paths the offline suite cannot exercise:
  * C-1 — the scheduler's Postgres advisory lock and the `SELECT ... FOR UPDATE` discovery claim.
  * H-4 — the Redis-backed idempotency store's atomic `SET NX` claim + replay.

They are SKIPPED unless the backends are provided via env, so the normal (SQLite) suite ignores
this directory entirely. The Postgres CI leg and the local Docker stack set:

    NEXUS_TEST_POSTGRES_URL=postgresql+asyncpg://nexus:nexus@localhost:5432/nexus
    NEXUS_TEST_REDIS_URL=redis://localhost:6379/0

This directory deliberately has NO SQLite-forcing conftest (unlike tests/), so the app's settings
resolve from the environment the CI/Docker leg provides.
"""
from __future__ import annotations

import os

import pytest

PG_URL = os.environ.get("NEXUS_TEST_POSTGRES_URL")
REDIS_URL = os.environ.get("NEXUS_TEST_REDIS_URL")

requires_pg = pytest.mark.skipif(not PG_URL, reason="NEXUS_TEST_POSTGRES_URL not set")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="NEXUS_TEST_REDIS_URL not set")
