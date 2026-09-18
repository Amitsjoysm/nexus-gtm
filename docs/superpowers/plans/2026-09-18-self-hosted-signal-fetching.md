# Self-hosted signal fetching — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop re-buying the same signal searches every six hours, then serve most of them from a self-hosted Scrapling fetcher instead of a paid search API — without changing what a rep sees.

**Architecture:** A platform-global `web_cache` table whose per-signal-kind TTL *is* the re-search cadence; a stateless `nexus-fetch` service on its own VM that fetches URLs and scrapes tolerant SERP endpoints; and a fallback chain (self-hosted → Firecrawl → in-process DuckDuckGo) so collection degrades rather than stops. Spec: `docs/superpowers/specs/2026-09-18-self-hosted-signal-fetching-design.md`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 async, Alembic, httpx, Scrapling 0.4.15 (`scrapling[fetchers]`), pytest (`asyncio_mode=auto`, `fresh_db` recreates tables per test).

**Phases:** A (tasks 1–6) is the cache and cadence — it saves money with the paid provider still primary and zero recall risk. B (tasks 7–13) builds and wires the service. C (tasks 14–15) shadows, promotes and documents.

---

## Facts the implementer needs before starting

- **Signal scans are metered nowhere.** `signal.news_scan`, `signal.rss_scan`, `signal.stored`, `inbox.task` and `automation.account_refresh` are priced in `nexus/billing/rates.py` and have **no `metered()` call site** anywhere in `nexus/ingestion/`. So the searches this plan removes cost us money and charge the customer nothing. **Do not add metering in this plan** — that would start charging customers for something they have never been charged for, which is a pricing decision for the product owner.
- **A provider that fails returns `[]`, not an exception.** `SearchProvider.last_failure` carries the reason. Anything caching results must read it: caching `[]` from a condemned key pool would freeze a failure in place for the whole TTL.
- **`query_dialect` decides how a dork renders.** `plain` is the safe floor; an `operator` query returns *zero* results on DuckDuckGo. A chain that can land on either must declare `plain`.
- **Platform-global tables carry no `tenant_id`** so `scripts/apply_rls.py` skips them, and every access goes through `get_platform_sessionmaker()`. Getting this wrong returns zero rows silently under Postgres RLS while passing every SQLite test.
- Run tests serially while iterating: `pytest tests/test_x.py -v -n0`.

---

# Phase A — durable cache and per-kind cadence

### Task 1: `web_cache` model and migration 0057

**Files:**
- Create: `nexus/models/web_cache.py`
- Modify: `nexus/models/__init__.py`
- Create: `migrations/versions/0057_web_cache.py`
- Test: `tests/test_web_cache_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_cache_model.py
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_web_cache_model.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.models.web_cache'`

- [ ] **Step 3: Write the model**

```python
# nexus/models/web_cache.py
"""Cached web fetches and search results, shared by every tenant.

Signal collection asks the same questions about the same company every six hours, and two
workspaces tracking one company ask them twice. The answer is the same fact for all of them, so it
is bought once and reused until it expires.

**Platform-global on purpose: no `tenant_id`.** `scripts/apply_rls.py` enrols any table that has
one, and enrolling this would return zero rows to the shared reader — a silent miss, not an error.
Nothing tenant-specific may ever be written here: a row is a public web result, keyed by the query
or URL that produced it.

`id` is a sha256 of the normalised subject plus the engine and limit, so two workers racing on one
query produce the same key and one insert loses cleanly instead of splitting the entry.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, TimestampMixin, TZDateTime

#: What a row holds. `search` is a list of hits; `page` is one fetched document.
CACHE_KINDS = ("search", "page")


class WebCache(TimestampMixin, Base):
    __tablename__ = "web_cache"
    __table_args__ = (
        # The prune sweep's only scan.
        Index("ix_web_cache_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="search")
    #: The normalised query or URL, readable so an operator can answer "what did we buy?".
    subject: Mapped[str] = mapped_column(Text, default="")
    #: Which backend answered, e.g. `nexusfetch:ddg` or `firecrawl`.
    engine: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict | list] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    #: Reuses, so the saving is measurable rather than asserted.
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
```

- [ ] **Step 4: Export it so `Base.metadata` sees it**

In `nexus/models/__init__.py`, add `web_cache` alongside the other model imports, following the
file's existing style (it imports each module so the mappers register).

```python
from nexus.models import web_cache  # noqa: F401
```

- [ ] **Step 5: Run the test again**

Run: `pytest tests/test_web_cache_model.py -v -n0`
Expected: PASS (2 passed)

- [ ] **Step 6: Write the migration**

```python
# migrations/versions/0057_web_cache.py
"""web_cache: one shared, expiring copy of every web answer we buy

Signal collection re-asks the same four questions about the same company every six hours, per
tenant. This stores the answer with an expiry, so the repeat is served from our own database.

Platform-global (no ``tenant_id``), like ``companies`` and ``people``: ``scripts/apply_rls.py``
enrols anything with one, and an enrolled cache would return zero rows to the shared reader.

Additive and empty on upgrade. An empty cache behaves exactly as today: every lookup misses and the
existing provider is called.

Revision ID: 0057_web_cache
Revises: 0056_alert_routing
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_web_cache"
down_revision = "0056_alert_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_cache",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="search"),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("engine", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_cache_expires", "web_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_web_cache_expires", table_name="web_cache")
    op.drop_table("web_cache")
```

- [ ] **Step 7: Prove the chain still replays onto an empty database**

Run: `pytest tests/test_migrations_replay.py -v -n0`
Expected: PASS. This builds a database from `alembic upgrade head` and diffs it against
`Base.metadata`, so a column mismatch between the model and the migration fails here.

- [ ] **Step 8: Commit**

```bash
git add nexus/models/web_cache.py nexus/models/__init__.py migrations/versions/0057_web_cache.py tests/test_web_cache_model.py
git commit -m "feat(fetching): web_cache table - one shared, expiring copy of every web answer"
```

---

### Task 2: the cache store

**Files:**
- Create: `nexus/fetching/__init__.py`
- Create: `nexus/fetching/cache.py`
- Test: `tests/test_web_cache_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_cache_store.py
"""The cache must be invisible when it works and harmless when it does not.

A cache that raises takes signal collection down with it, so every entry point here is total: a
failure is a miss. The expiry is the product decision (how often we re-ask), so it is tested
explicitly rather than assumed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.fetching import cache
from nexus.models.web_cache import WebCache

HITS = [{"title": "Acme raises $40M Series B", "url": "https://x.test/a", "snippet": "", "source": "t"}]


async def test_a_miss_returns_none():
    assert await cache.get("search", "acme funding", engine="t", limit=4) is None


async def test_a_stored_answer_is_served_and_counted():
    await cache.put("search", "acme funding", engine="t", limit=4, payload=HITS, ttl_s=3600)

    assert await cache.get("search", "acme funding", engine="t", limit=4) == HITS

    async with get_platform_sessionmaker()() as s:
        row = await s.get(WebCache, cache.cache_key("search", "acme funding", engine="t", limit=4))
    assert row.hit_count == 1
    assert row.bytes > 0


async def test_an_expired_answer_is_a_miss():
    await cache.put("search", "old", engine="t", limit=4, payload=HITS, ttl_s=-1)
    assert await cache.get("search", "old", engine="t", limit=4) is None


async def test_the_key_separates_engine_and_limit():
    await cache.put("search", "same", engine="a", limit=4, payload=HITS, ttl_s=3600)
    assert await cache.get("search", "same", engine="b", limit=4) is None
    assert await cache.get("search", "same", engine="a", limit=6) is None


async def test_a_second_put_replaces_rather_than_duplicating():
    await cache.put("search", "q", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "q", engine="t", limit=4, payload=[], ttl_s=3600)
    assert await cache.get("search", "q", engine="t", limit=4) == []


async def test_a_broken_database_is_a_miss_not_an_exception(monkeypatch):
    def boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(cache, "get_platform_sessionmaker", boom)
    assert await cache.get("search", "q", engine="t", limit=4) is None
    # put must not raise either: nothing is cached, collection continues.
    await cache.put("search", "q", engine="t", limit=4, payload=HITS, ttl_s=3600)


async def test_prune_deletes_only_expired_rows():
    await cache.put("search", "fresh", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "stale", engine="t", limit=4, payload=HITS, ttl_s=-1)

    deleted = await cache.prune()

    assert deleted == 1
    assert await cache.get("search", "fresh", engine="t", limit=4) == HITS
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_web_cache_store.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching'`

- [ ] **Step 3: Write the store**

```python
# nexus/fetching/__init__.py
"""Self-hosted fetching: the shared web cache and the client for the nexus-fetch service."""
```

```python
# nexus/fetching/cache.py
"""Durable, cross-tenant cache for searches and page fetches.

Every entry point is TOTAL. A cache exists to make collection cheaper, so it must never be able to
make collection fail: an unreachable database, a serialisation error or a schema surprise all
resolve to "miss", and the caller buys the answer as it does today.

Platform-global storage, so a company crawled for one tenant is not re-bought for the next. See
`nexus/models/web_cache.py` for why there is no `tenant_id`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import delete, select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.web_cache import WebCache

logger = logging.getLogger("nexus.fetching.cache")


def cache_key(kind: str, subject: str, *, engine: str = "", limit: int = 0) -> str:
    """Deterministic id, so two workers racing on one query write the same row."""
    raw = "|".join(
        [(kind or "").strip().lower(), (subject or "").strip().lower(),
         (engine or "").strip().lower(), str(int(limit or 0))]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enabled() -> bool:
    from nexus.core.config import get_settings

    return bool(get_settings().web_cache_enabled)


async def get(kind: str, subject: str, *, engine: str = "", limit: int = 0):
    """The cached payload, or ``None`` when there is nothing fresh to serve."""
    if not enabled():
        return None
    key = cache_key(kind, subject, engine=engine, limit=limit)
    try:
        async with get_platform_sessionmaker()() as session:
            row = await session.get(WebCache, key)
            if row is None or row.expires_at <= utcnow():
                return None
            row.hit_count = (row.hit_count or 0) + 1
            await session.commit()
            return row.payload
    except Exception:  # a cache must never break the caller
        logger.warning("web cache read failed for %s", key, exc_info=True)
        return None


async def put(kind: str, subject: str, *, engine: str = "", limit: int = 0,
              payload, ttl_s: float) -> None:
    """Store an answer for ``ttl_s`` seconds. Replaces any existing row for the same key."""
    if not enabled():
        return
    key = cache_key(kind, subject, engine=engine, limit=limit)
    now = utcnow()
    try:
        size = len(json.dumps(payload).encode("utf-8"))
    except Exception:
        size = 0
    try:
        async with get_platform_sessionmaker()() as session:
            row = await session.get(WebCache, key)
            if row is None:
                row = WebCache(id=key, kind=kind, subject=(subject or "")[:2000])
                session.add(row)
            row.engine = (engine or "")[:64]
            row.payload = payload
            row.fetched_at = now
            row.expires_at = now + timedelta(seconds=float(ttl_s))
            row.bytes = size
            await session.commit()
    except Exception:
        logger.warning("web cache write failed for %s", key, exc_info=True)


async def prune() -> int:
    """Delete expired rows. Returns how many went. Never raises."""
    try:
        async with get_platform_sessionmaker()() as session:
            result = await session.execute(
                delete(WebCache).where(WebCache.expires_at <= utcnow())
            )
            await session.commit()
            return int(result.rowcount or 0)
    except Exception:
        logger.warning("web cache prune failed", exc_info=True)
        return 0
```

- [ ] **Step 4: Add the settings flag the store reads**

In `nexus/core/config.py`, beside the other signal settings (around `signal_dork_max_queries`):

```python
    # ---- Self-hosted fetching -------------------------------------------------------------
    #: Serve repeated searches and page fetches from `web_cache` instead of re-buying them.
    #: Off means every lookup misses, which is exactly the behaviour before the cache existed.
    web_cache_enabled: bool = True
    #: Re-ask window for time-critical kinds (funding, news).
    signal_cache_ttl_fast_s: int = 21600     # 6h — matches the HOT refresh interval
    #: Re-ask window for everything else searched (hiring, job postings, tech adoption).
    signal_cache_ttl_slow_s: int = 86400     # 24h
    #: Re-fetch window for a page (website watch, careers pages).
    page_cache_ttl_s: int = 86400            # 24h
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_web_cache_store.py -v -n0`
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add nexus/fetching/__init__.py nexus/fetching/cache.py nexus/core/config.py tests/test_web_cache_store.py
git commit -m "feat(fetching): shared web cache store - total on failure, expiry-driven"
```

---

### Task 3: per-kind TTL policy

**Files:**
- Create: `nexus/fetching/ttl.py`
- Test: `tests/test_signal_cache_ttl.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_cache_ttl.py
"""The TTL is the cadence: how long we wait before asking the web the same question again.

Funding and news keep the six-hour loop because being first is the value. Everything else moves to
a day, which is where the saving comes from. The values are settings, so a cadence can be tightened
from the Control plane without a deploy — which means the policy must READ them per call, not
capture them at import.
"""
from __future__ import annotations

from nexus.fetching.ttl import ttl_for_page, ttl_for_signal_kind


def test_time_critical_kinds_keep_the_six_hour_loop():
    assert ttl_for_signal_kind("funding") == 21600
    assert ttl_for_signal_kind("news") == 21600


def test_slower_kinds_move_to_a_day():
    for kind in ("job_posting", "hiring", "tech_install", "website_change"):
        assert ttl_for_signal_kind(kind) == 86400


def test_an_unknown_kind_is_treated_as_slow():
    # Being wrong here costs freshness on one kind; the opposite default costs money on all of them.
    assert ttl_for_signal_kind("") == 86400
    assert ttl_for_signal_kind("something_new") == 86400


def test_a_changed_setting_reaches_the_next_call(monkeypatch):
    from nexus.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_cache_ttl_fast_s", 60, raising=False)
    assert ttl_for_signal_kind("funding") == 60


def test_pages_have_their_own_window():
    assert ttl_for_page() == 86400
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_signal_cache_ttl.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching.ttl'`

- [ ] **Step 3: Write the policy**

```python
# nexus/fetching/ttl.py
"""How long a cached web answer stays good, by signal kind.

The TTL IS the cadence. There is no scheduler change anywhere in this feature: a dork whose cached
entry is still fresh short-circuits before any request, so `next_refresh_at` and `tiering.classify`
keep working exactly as they do today and the account's timeline is unchanged.

Settings are read on every call. This module is used from the process-wide ingestion service, which
is built once and keeps its sources, so a value captured at import would ignore the Control plane
until a restart — the same trap `DorkedSearchSource` documents for its query cap.
"""
from __future__ import annotations

#: Kinds where being first is the value, so they keep the six-hour loop.
FAST_KINDS = frozenset({"funding", "news"})


def ttl_for_signal_kind(kind: str) -> int:
    """Seconds a searched answer for this signal kind stays good."""
    from nexus.core.config import get_settings

    settings = get_settings()
    if (kind or "").strip().lower() in FAST_KINDS:
        return int(settings.signal_cache_ttl_fast_s)
    return int(settings.signal_cache_ttl_slow_s)


def ttl_for_page() -> int:
    """Seconds a fetched page stays good."""
    from nexus.core.config import get_settings

    return int(get_settings().page_cache_ttl_s)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_signal_cache_ttl.py -v -n0`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add nexus/fetching/ttl.py tests/test_signal_cache_ttl.py
git commit -m "feat(fetching): per-kind cache TTL - the TTL is the re-search cadence"
```

---

### Task 4: expose the new settings in the Control plane

**Files:**
- Modify: `nexus/runtime_config/catalog.py`
- Test: `tests/test_runtime_control_plane.py` (existing structural guards)

- [ ] **Step 1: Run the existing guards to see them fail**

Run: `pytest tests/test_runtime_control_plane.py -v -n0`
Expected: PASS today. These guards assert that every catalog key is read outside `config.py` and
that every free-text setting has a validator — they are what will catch a half-wired setting once
you add entries in Step 2.

- [ ] **Step 2: Add the settings to the catalog**

In `nexus/runtime_config/catalog.py`, in the signals group, following the existing `SettingSpec`
style (every entry states `effect`, and anything medium or high risk states `warning`):

```python
    SettingSpec(
        key="web_cache_enabled", label="Reuse web answers", group=SIGNALS, kind="bool",
        effect="Serve a repeated search or page fetch from our own database instead of buying it "
               "again. Off means every lookup goes to the provider, as it did before the cache "
               "existed.",
        warning="Turning this off multiplies signal search spend by roughly four for every hot "
                "account, because the same queries are re-bought on every six-hour refresh.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_cache_ttl_fast_s", label="Re-ask window: funding and news (seconds)",
        group=SIGNALS, kind="int", minimum=600, maximum=604800,
        effect="How long a funding or news search result is reused before the web is asked again.",
        warning="Raising this delays time-critical signals: a funding round found by open-web news "
                "can reach the rep this much later than the company announced it.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_cache_ttl_slow_s", label="Re-ask window: hiring and tech (seconds)",
        group=SIGNALS, kind="int", minimum=600, maximum=604800,
        effect="How long a hiring, job-posting or technology search result is reused. The ATS board "
               "and the company's own site are fetched first-party and are not affected.",
        warning="Lowering this towards the funding window removes most of the saving, because these "
                "kinds are the bulk of the queries.",
        risk="medium",
    ),
    SettingSpec(
        key="page_cache_ttl_s", label="Re-fetch window: pages (seconds)", group=SIGNALS,
        kind="int", minimum=600, maximum=604800,
        effect="How long a fetched page (website watch, careers page) is reused before it is "
               "fetched again.",
        warning="Below the website-watch baseline this reports the same change repeatedly; far "
                "above it, a pricing-page change is noticed late.",
        risk="medium",
    ),
```

- [ ] **Step 3: Run the guards again**

Run: `pytest tests/test_runtime_control_plane.py -v -n0`
Expected: PASS. If "every catalog key is read outside config.py" fails, the setting is not wired —
check Tasks 2 and 3 are committed first.

- [ ] **Step 4: Commit**

```bash
git add nexus/runtime_config/catalog.py
git commit -m "feat(control-plane): cache switch and per-kind re-ask windows"
```

---

### Task 5: serve dork searches from the cache

**Files:**
- Modify: `nexus/ingestion/sources.py` (`DorkedSearchSource._run` and `DorkedSearchSource.fetch`)
- Test: `tests/test_signal_search_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_search_cache.py
"""A repeated dork is served from `web_cache`, and a FAILED search is never cached.

The second half is the subtle one. A provider whose key pool is condemned returns `[]` and sets
`last_failure` — it does not raise. Caching that empty list would freeze the outage in place for
the whole TTL, and the account would report "no signals" long after the key was fixed.
"""
from __future__ import annotations

import pytest

from nexus.fetching import cache
from nexus.ingestion.sources import DorkedSearchSource
from nexus.integrations.search.provider import SearchHit
from nexus.models.account import Account


class CountingProvider:
    """Records every query it is asked for, so 'was this bought again?' is a length check."""

    name = "counting"
    query_dialect = "plain"
    last_failure = ""

    def __init__(self, hits=None):
        self.queries: list[str] = []
        self._hits = hits if hits is not None else [
            SearchHit(title="Acme raises $40M Series B", url="https://x.test/a", snippet="", source="counting")
        ]

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.queries.append(query)
        return list(self._hits)


def _account() -> Account:
    return Account(id="a1", tenant_id="t1", name="Acme", domain="acme.test", industry="Software")


async def test_a_second_crawl_buys_nothing():
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=2)

    first = await source.fetch(_account())
    bought_first = len(provider.queries)
    second = await source.fetch(_account())

    assert bought_first == 2
    assert len(provider.queries) == 2, "the second crawl re-bought a query it already had"
    assert [s.title for s in second] == [s.title for s in first]


async def test_the_cached_run_is_marked_in_provenance():
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert source.last_provenance["queries"][0]["cached"] is True


async def test_a_failed_search_is_not_cached():
    class FailingProvider(CountingProvider):
        last_failure = "all keys rejected"

        async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
            self.queries.append(query)
            return []

    provider = FailingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 2, "an outage was cached and replayed as 'no results'"


async def test_a_genuine_no_results_answer_is_cached():
    provider = CountingProvider(hits=[])
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 1


async def test_the_cache_can_be_switched_off(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "web_cache_enabled", False, raising=False)
    provider = CountingProvider()
    source = DorkedSearchSource(search=provider, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert len(provider.queries) == 2
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_signal_search_cache.py -v -n0`
Expected: FAIL on `test_a_second_crawl_buys_nothing` — `2 != 4`, because every crawl buys every
query today.

- [ ] **Step 3: Thread the signal kind into `_run` and consult the cache**

In `nexus/ingestion/sources.py`, change `_run`'s signature and body:

```python
    async def _run(self, query: str, include: tuple, exclude: tuple,
                   *, kind: str = "") -> list[dict] | None:
        """Hits for one dork, or None if the provider itself failed.

        None is not the same as no results, and the caller treats it differently: the keyless
        DuckDuckGo backend starts returning 403 after roughly ten rapid queries, and continuing to
        fire the rest of the batch only deepens the block while returning nothing.

        A fresh cached answer short-circuits before any request — that reuse IS the per-kind
        cadence (`nexus/fetching/ttl.py`). Only a SUCCESSFUL answer is stored: a provider whose key
        pool is condemned returns `[]` with `last_failure` set, and caching that would replay an
        outage as "no results" for the whole TTL.
        """
        from nexus.fetching import cache
        from nexus.fetching.ttl import ttl_for_signal_kind

        provider = self._provider()
        engine = getattr(provider, "name", "unknown")
        cached = await cache.get("search", query, engine=engine, limit=self._per_query)
        if cached is not None:
            self._last_run_cached = True
            return list(cached)
        self._last_run_cached = False
        try:
            hits = await provider.search_recent(
                query,
                limit=self._per_query,
                days=self._recency_days,
                include_domains=include,
                exclude_domains=exclude,
            )
        except TypeError:
            # An injected double or an older provider without the structured-domain kwargs. The
            # keyword dialect already carries the domains inline, so this loses nothing there.
            try:
                hits = await provider.search_recent(
                    query, limit=self._per_query, days=self._recency_days
                )
            except Exception:
                logger.warning("dork search failed: %s", query, exc_info=True)
                return None
        except Exception:
            logger.warning("dork search failed: %s", query, exc_info=True)
            return None
        out = []
        for h in hits or []:
            # SearchHit dataclass or a plain dict, depending on the seam an injected double uses.
            out.append(h.as_dict() if hasattr(h, "as_dict") else dict(h))
        if not getattr(provider, "last_failure", ""):
            await cache.put(
                "search", query, engine=engine, limit=self._per_query,
                payload=out, ttl_s=ttl_for_signal_kind(kind),
            )
        return out
```

Add the attribute to `__init__` beside `last_provenance`:

```python
        #: Whether the last `_run` was served from the cache, for provenance.
        self._last_run_cached = False
```

- [ ] **Step 4: Pass the kind and record the reuse in provenance**

In `DorkedSearchSource.fetch`, at the call site:

```python
            hits = await self._run(query, include, exclude, kind=dork.kind)
            self.last_provenance["queries"][-1]["cached"] = self._last_run_cached
```

Keep the existing `if hits is None:` branch immediately after, unchanged.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_signal_search_cache.py -v -n0`
Expected: PASS (5 passed)

- [ ] **Step 6: Run the signal suite for regressions**

Run: `pytest tests/test_signal_classifier.py tests/test_dork_source.py tests/test_ingestion_service.py -v -n0`
Expected: PASS. If a test file name differs, run `pytest tests -k "dork or signal or ingestion" -n0`.

- [ ] **Step 7: Commit**

```bash
git add nexus/ingestion/sources.py tests/test_signal_search_cache.py
git commit -m "feat(signals): serve repeated dork searches from the shared cache"
```

---

### Task 6: prune expired rows on the heartbeat

**Files:**
- Modify: `nexus/workers/tasks.py`
- Modify: `nexus/workers/scheduler.py`
- Test: `tests/test_web_cache_prune_job.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_cache_prune_job.py
"""Expired rows must actually leave, or the table grows forever and the saving turns into storage."""
from __future__ import annotations

from nexus.fetching import cache
from nexus.workers.tasks import HANDLERS, handle_prune_web_cache

HITS = [{"title": "t", "url": "https://x.test/a", "snippet": "", "source": "t"}]


async def test_the_job_deletes_expired_rows_only():
    await cache.put("search", "fresh", engine="t", limit=4, payload=HITS, ttl_s=3600)
    await cache.put("search", "stale", engine="t", limit=4, payload=HITS, ttl_s=-1)

    result = await handle_prune_web_cache({})

    assert result == {"pruned": 1}
    assert await cache.get("search", "fresh", engine="t", limit=4) == HITS


def test_the_job_is_routable():
    # An unregistered name is dropped by `dispatch` with "no handler for job", silently.
    assert HANDLERS["prune_web_cache"] is handle_prune_web_cache
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_web_cache_prune_job.py -v -n0`
Expected: FAIL, `ImportError: cannot import name 'handle_prune_web_cache'`

- [ ] **Step 3: Add the handler**

In `nexus/workers/tasks.py`, beside the other sweeps:

```python
async def handle_prune_web_cache(payload: dict) -> dict:
    """Delete expired `web_cache` rows.

    Idempotent and bounded by the index on `expires_at`, so the heartbeat may enqueue it every
    tick. Never raises: `cache.prune` returns 0 on any failure, because a cache that cannot tidy
    itself must not fail a job that other work is queued behind.
    """
    from nexus.fetching import cache

    return {"pruned": await cache.prune()}
```

Register it in `HANDLERS`:

```python
    "prune_web_cache": handle_prune_web_cache,
```

- [ ] **Step 4: Add the enqueue helper and schedule it**

In `nexus/workers/tasks.py`, next to the other `enqueue_*` helpers, following their exact shape:

```python
async def enqueue_prune_web_cache(queue) -> None:
    """Heartbeat driver for the cache sweep."""
    await queue.enqueue(Job(name="prune_web_cache", payload={}))
```

In `nexus/workers/scheduler.py`, import it with the other drivers and enqueue it in `_enqueue_due`
alongside `enqueue_rollup_usage`.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_web_cache_prune_job.py -v -n0`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add nexus/workers/tasks.py nexus/workers/scheduler.py tests/test_web_cache_prune_job.py
git commit -m "feat(fetching): prune expired web_cache rows on the heartbeat"
```

**Phase A is now shippable on its own**: the paid provider is still primary, nothing about recall
changed, and repeat searches stop being bought.

---

# Phase B — the self-hosted fetcher

### Task 7: the SSRF guard and the shared secret

**Files:**
- Create: `nexus/fetching/guard.py`
- Test: `tests/test_fetch_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_guard.py
"""A service that fetches any URL you name is an open proxy and a read oracle.

`nexus/sources/safety.py` makes this argument about DSNs; it is the same argument, so the same
rules apply. Resolve-then-check, because a public name can point at loopback.
"""
from __future__ import annotations

import pytest

from nexus.fetching.guard import UrlRejected, check_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://metadata.google.internal/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "file:///etc/passwd",
    "gopher://x.test/",
])
def test_private_and_non_http_targets_are_refused(url):
    with pytest.raises(UrlRejected):
        check_url(url)


def test_a_public_page_is_allowed():
    assert check_url("https://acme.test/pricing").startswith("https://")


def test_a_url_with_credentials_is_refused():
    # user:pass@host is how an SSRF payload smuggles a different authority past a naive parser.
    with pytest.raises(UrlRejected):
        check_url("https://user:pass@acme.test/")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_fetch_guard.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching.guard'`

- [ ] **Step 3: Write the guard**

```python
# nexus/fetching/guard.py
"""What the fetcher is allowed to request.

An endpoint that takes a URL and reports what came back is a port scanner and a read oracle when it
is pointed at the container network or a metadata endpoint. `nexus/sources/safety.py` refuses
private DSNs for exactly this reason; this is the same rule for the same threat.

Resolve, THEN check: a public hostname can resolve to loopback, and checking the name alone is the
bypass.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: Names that resolve to cloud metadata services, refused whatever they resolve to today.
_METADATA_NAMES = frozenset({
    "metadata.google.internal", "metadata.goog", "instance-data",
})


class UrlRejected(ValueError):
    """The URL may not be fetched. The message names the reason, for the operator."""


def _blocked_address(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ""  # unresolvable is the caller's problem, not a safety refusal
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            return str(address)
    return ""


def check_url(url: str) -> str:
    """Return the URL when it is safe to fetch; raise :class:`UrlRejected` when it is not."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UrlRejected(f"scheme {parts.scheme or '(none)'} is not fetchable")
    if parts.username or parts.password:
        raise UrlRejected("credentials in a URL are not accepted")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlRejected("no host in URL")
    if host in _METADATA_NAMES:
        raise UrlRejected("metadata hosts are never fetchable")
    blocked = _blocked_address(host)
    if blocked:
        raise UrlRejected(f"{host} resolves to {blocked}, which is not fetchable")
    return url.strip()
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_fetch_guard.py -v -n0`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add nexus/fetching/guard.py tests/test_fetch_guard.py
git commit -m "feat(fetching): SSRF guard - resolve then check, metadata and credentials refused"
```

---

### Task 8: SERP parsing, tested against saved HTML

**Files:**
- Create: `nexus/fetching/parse.py`
- Create: `tests/fixtures/serp_ddg.html` (save a real DuckDuckGo HTML result page)
- Test: `tests/test_serp_parse.py`

- [ ] **Step 1: Save a fixture**

```bash
curl -s -A "Mozilla/5.0" "https://html.duckduckgo.com/html/?q=vanta+funding" -o tests/fixtures/serp_ddg.html
```

Open it and confirm it contains result links (`result__a`). If it returns a block page, wait a
minute and retry — a block page is itself worth saving later as `serp_ddg_blocked.html` for Step 4.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_serp_parse.py
"""Parsing is tested against a saved page, never the network.

The engines change their markup; when they do, this test fails with a real diff instead of the
product quietly reporting "no signals". `blocked` is a distinct outcome from "no results" for the
same reason `signal_source_runs` separates `error` from `empty`.
"""
from __future__ import annotations

from pathlib import Path

from nexus.fetching.parse import BlockedByEngine, parse_ddg

FIXTURE = Path(__file__).parent / "fixtures" / "serp_ddg.html"


def test_results_are_extracted_with_titles_and_absolute_urls():
    hits = parse_ddg(FIXTURE.read_text(encoding="utf-8"), limit=5)

    assert 1 <= len(hits) <= 5
    for hit in hits:
        assert hit["title"]
        assert hit["url"].startswith("http")
        assert "duckduckgo.com/l/" not in hit["url"], "redirect wrapper was not unwrapped"


def test_an_anti_bot_page_raises_blocked_rather_than_returning_nothing():
    blocked_page = "<html><body>Unfortunately, bots use DuckDuckGo too. Please try again.</body></html>"
    try:
        parse_ddg(blocked_page, limit=5)
    except BlockedByEngine:
        return
    raise AssertionError("a block page was read as 'no results'")
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_serp_parse.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching.parse'`

- [ ] **Step 4: Write the parser**

```python
# nexus/fetching/parse.py
"""Turn a search-results page into hits.

Lives in the main package rather than in the service so the parsing is covered by the offline
suite: markup changes are the thing most likely to break silently, and a parser that only runs on
the VM is a parser nobody tests.

`BlockedByEngine` is deliberately not an empty list. "The engine refused" and "the market is quiet"
must never look alike — the distinction `signal_source_runs` draws between `error` and `empty`, and
the reason a source that finds nothing forever is detectable at all.
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlsplit

_TAG = re.compile(r"<[^>]+>")
_RESULT = re.compile(r'result__a[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.DOTALL)
_SNIPPET = re.compile(r'result__snippet[^>]*>(?P<snippet>.*?)</a>', re.DOTALL)
#: Phrases the HTML endpoint serves instead of results when it has decided we are a bot.
_BLOCK_MARKERS = ("bots use duckduckgo too", "unusual traffic", "please try again")


class BlockedByEngine(RuntimeError):
    """The engine served an anti-bot page. Not the same as finding nothing."""


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).strip()


def _absolute(href: str) -> str:
    if "uddg=" in href:
        query = parse_qs(urlsplit(href if href.startswith("http") else "https:" + href).query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
    return href if href.startswith("http") else "https:" + href


def parse_ddg(body: str, *, limit: int = 5) -> list[dict]:
    """Hits from a DuckDuckGo HTML results page."""
    lowered = (body or "").lower()
    matches = list(_RESULT.finditer(body or ""))
    if not matches and any(marker in lowered for marker in _BLOCK_MARKERS):
        raise BlockedByEngine("duckduckgo served an anti-bot page")
    snippets = _SNIPPET.findall(body or "")
    hits: list[dict] = []
    for index, match in enumerate(matches):
        if index >= limit:
            break
        hits.append({
            "title": _clean(match.group("title")),
            "url": _absolute(match.group("url")),
            "snippet": _clean(snippets[index]) if index < len(snippets) else "",
            "source": "nexusfetch:ddg",
        })
    return hits
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_serp_parse.py -v -n0`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add nexus/fetching/parse.py tests/test_serp_parse.py tests/fixtures/serp_ddg.html
git commit -m "feat(fetching): SERP parsing with a distinct blocked outcome, tested on a saved page"
```

---

### Task 9: the `nexus-fetch` service

**Files:**
- Create: `services/fetch/app.py`
- Create: `services/fetch/Dockerfile`
- Create: `services/fetch/docker-compose.yml`
- Create: `services/fetch/README.md`
- Modify: `pyproject.toml` (a `fetch` extra)
- Test: `tests/test_fetch_service_app.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_service_app.py
"""The service refuses what it must, and never returns a fabricated answer.

Scrapling is not installed in the test environment, so the fetchers are injected. That is
deliberate: the offline suite must never reach the network.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from services.fetch.app import build_app

TOKEN = "test-token"


def _app(search=None, fetch=None):
    return build_app(token=TOKEN, search_fn=search, fetch_fn=fetch)


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://fetch.test")


async def test_a_call_without_the_token_is_refused():
    async with await _client(_app()) as client:
        response = await client.post("/search", json={"query": "acme funding", "limit": 3})
    assert response.status_code == 401


async def test_a_private_target_is_refused_even_with_the_token():
    async with await _client(_app()) as client:
        response = await client.post(
            "/fetch", json={"url": "http://169.254.169.254/latest/meta-data/"},
            headers={"X-Fetch-Token": TOKEN},
        )
    assert response.status_code == 400
    assert "not fetchable" in response.json()["detail"]


async def test_search_returns_hits_and_names_the_engine():
    async def search(query, *, limit, recency_days):
        return [{"title": "Acme raises $40M", "url": "https://x.test/a", "snippet": "",
                 "source": "nexusfetch:ddg"}]

    async with await _client(_app(search=search)) as client:
        response = await client.post(
            "/search", json={"query": "acme funding", "limit": 3},
            headers={"X-Fetch-Token": TOKEN},
        )
    body = response.json()
    assert response.status_code == 200
    assert body["engine"] == "nexusfetch:ddg"
    assert body["hits"][0]["url"] == "https://x.test/a"


async def test_a_blocked_engine_answers_503_not_an_empty_list():
    from nexus.fetching.parse import BlockedByEngine

    async def search(query, *, limit, recency_days):
        raise BlockedByEngine("duckduckgo served an anti-bot page")

    async with await _client(_app(search=search)) as client:
        response = await client.post(
            "/search", json={"query": "acme funding", "limit": 3},
            headers={"X-Fetch-Token": TOKEN},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "blocked"


async def test_health_reports_without_a_token():
    async with await _client(_app()) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_fetch_service_app.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'services'`

- [ ] **Step 3: Write the service**

```python
# services/fetch/__init__.py
```

```python
# services/fetch/app.py
"""nexus-fetch — a stateless fetcher for signal collection.

It does two things: fetch one URL, and scrape one search-results page. It holds no data, so the VM
it runs on can be destroyed and rebuilt at any time, and it stores nothing about a tenant — the
caller decides what to keep.

**Never publicly reachable.** A shared secret on every call, a firewall to the app's egress
addresses, and `nexus.fetching.guard` refusing private, loopback, link-local and metadata targets.
An open fetch endpoint is an open proxy.

Scrapling is imported lazily so this module can be tested — and the fetchers injected — without the
browser stack installed.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from nexus.fetching.guard import UrlRejected, check_url
from nexus.fetching.parse import BlockedByEngine, parse_ddg

logger = logging.getLogger("nexus_fetch")

#: One in-flight request per host at a time, so we never look like a burst to one target.
_HOST_LOCKS: dict[str, asyncio.Lock] = {}
#: Total concurrent fetches, so a bug in the caller cannot melt the box or the targets.
_GLOBAL = asyncio.Semaphore(int(os.environ.get("FETCH_CONCURRENCY", "4")))


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)
    recency_days: int = Field(default=0, ge=0, le=3650)


class FetchIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    mode: str = Field(default="http", pattern="^(http|stealth)$")
    timeout_s: float = Field(default=20.0, ge=1, le=60)


async def _default_search(query: str, *, limit: int, recency_days: int) -> list[dict]:
    """Scrape the DuckDuckGo HTML endpoint with Scrapling's impersonating fetcher."""
    from scrapling.fetchers import AsyncFetcher  # imported lazily: not installed in tests

    from urllib.parse import quote_plus

    page = await AsyncFetcher.get(
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", stealthy_headers=True
    )
    body = getattr(page, "html_content", None) or getattr(page, "body", "") or str(page)
    return parse_ddg(body, limit=limit)


async def _default_fetch(url: str, *, mode: str, timeout_s: float) -> dict:
    if mode == "stealth":
        from scrapling.fetchers import StealthyFetcher

        page = await StealthyFetcher.async_fetch(url, timeout=timeout_s * 1000)
    else:
        from scrapling.fetchers import AsyncFetcher

        page = await AsyncFetcher.get(url, timeout=timeout_s, stealthy_headers=True)
    html_content = getattr(page, "html_content", None) or getattr(page, "body", "") or str(page)
    return {
        "status": int(getattr(page, "status", 200) or 200),
        "final_url": str(getattr(page, "url", url) or url),
        "html": html_content,
        "text": (page.get_all_text() if hasattr(page, "get_all_text") else ""),
        "title": (page.css_first("title::text") if hasattr(page, "css_first") else "") or "",
    }


def build_app(*, token: str = "", search_fn=None, fetch_fn=None) -> FastAPI:
    """The ASGI app. `search_fn`/`fetch_fn` are injectable so tests never touch the network."""
    token = token or os.environ.get("FETCH_TOKEN", "")
    search_fn = search_fn or _default_search
    fetch_fn = fetch_fn or _default_fetch
    app = FastAPI(title="nexus-fetch", docs_url=None, redoc_url=None, openapi_url=None)

    def _authorize(supplied: str | None) -> None:
        if not token or supplied != token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing token")

    def _lock(host: str) -> asyncio.Lock:
        return _HOST_LOCKS.setdefault(host, asyncio.Lock())

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "browser": _browser_available()}

    @app.post("/search")
    async def search(body: SearchIn, x_fetch_token: str | None = Header(default=None)) -> dict:
        _authorize(x_fetch_token)
        async with _GLOBAL, _lock("duckduckgo.com"):
            try:
                hits = await search_fn(body.query, limit=body.limit, recency_days=body.recency_days)
            except BlockedByEngine:
                # 503, never an empty list: a refusal must not read as a quiet market.
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "blocked") from None
            except Exception as exc:
                logger.warning("search failed: %r", exc)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "search failed") from None
        engine = hits[0].get("source", "nexusfetch") if hits else "nexusfetch:ddg"
        return {"engine": engine, "hits": hits}

    @app.post("/fetch")
    async def fetch(body: FetchIn, x_fetch_token: str | None = Header(default=None)) -> dict:
        _authorize(x_fetch_token)
        try:
            url = check_url(body.url)
        except UrlRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        async with _GLOBAL, _lock(host):
            try:
                return await fetch_fn(url, mode=body.mode, timeout_s=body.timeout_s)
            except Exception as exc:
                logger.warning("fetch failed for %s: %r", url, exc)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "fetch failed") from None

    return app


def _browser_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("scrapling") is not None


app = build_app()
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_fetch_service_app.py -v -n0`
Expected: PASS (5 passed)

- [ ] **Step 5: Add the extra and the container**

In `pyproject.toml`, beside the existing `scraping` extra:

```toml
fetch = ["scrapling[fetchers]>=0.4.15", "fastapi>=0.110", "uvicorn>=0.29"]
```

```dockerfile
# services/fetch/Dockerfile
FROM python:3.11-slim

# Scrapling's browser tier needs the Playwright system libraries; the HTTP tier does not, but the
# image carries both so `mode=stealth` works without a rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml constraints.txt README.md ./
COPY nexus ./nexus
COPY services ./services

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -c constraints.txt ".[fetch]" \
    && python -m playwright install --with-deps chromium \
    && scrapling install

ENV FETCH_CONCURRENCY=4
EXPOSE 8081
CMD ["uvicorn", "services.fetch.app:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
```

```yaml
# services/fetch/docker-compose.yml
# Runs on its own small VM, NOT on the Reacher box: scraping egress must never affect email
# verification, which billing depends on.
services:
  fetch:
    build:
      context: ../..
      dockerfile: services/fetch/Dockerfile
    restart: unless-stopped
    environment:
      FETCH_TOKEN: ${FETCH_TOKEN:?set a long random token}
      FETCH_CONCURRENCY: "4"
    ports:
      # Bind to the private interface only; the firewall allows just the app's egress addresses.
      - "127.0.0.1:8081:8081"
    mem_limit: 2g
```

- [ ] **Step 6: Write the operator README**

```markdown
<!-- services/fetch/README.md -->
# nexus-fetch

Stateless fetcher for signal collection. Holds no data; rebuild it freely.

## Deploy

1. Provision a small VM (2 vCPU, 2–4 GB). **Not the Reacher box** — scraping egress must not share
   an IP with email verification.
2. `export FETCH_TOKEN=$(openssl rand -hex 32)` and store it in the app's `deploy/.env` as
   `NEXUS_FETCH_TOKEN`.
3. `docker compose -f services/fetch/docker-compose.yml up -d --build`
4. Firewall: allow 8081 only from the app's egress addresses. Never expose it publicly — it is an
   open proxy if anyone else can reach it.
5. Point the app at it: `NEXUS_FETCH_SERVICE_URL=http://<vm-ip>:8081`.

## Check it

    curl -s localhost:8081/health
    curl -s -X POST localhost:8081/search -H "X-Fetch-Token: $FETCH_TOKEN" \
         -H 'content-type: application/json' -d '{"query":"vanta funding","limit":3}'

A `503 blocked` means the engine served an anti-bot page — expected occasionally, and the app falls
back to the paid provider. Sustained blocks mean the pacing is too aggressive or the IP is burnt.
```

- [ ] **Step 7: Commit**

```bash
git add services pyproject.toml tests/test_fetch_service_app.py
git commit -m "feat(fetch-service): stateless nexus-fetch service - token, SSRF guard, paced hosts"
```

---

### Task 10: the client the app calls

**Files:**
- Create: `nexus/fetching/client.py`
- Modify: `nexus/core/config.py`
- Test: `tests/test_fetch_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_client.py
"""The client never raises across the boundary, and says WHY it could not answer.

`last_failure` is what stops a dead service from looking like a quiet market — the same contract
`SearchProvider` already defines.
"""
from __future__ import annotations

import httpx

from nexus.fetching.client import FetchClient


def _client(handler) -> FetchClient:
    transport = httpx.MockTransport(handler)
    return FetchClient(base_url="http://fetch.test", token="t",
                       http=httpx.AsyncClient(transport=transport, base_url="http://fetch.test"))


async def test_search_returns_hits_and_sends_the_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Fetch-Token")
        return httpx.Response(200, json={"engine": "nexusfetch:ddg", "hits": [
            {"title": "Acme raises $40M", "url": "https://x.test/a", "snippet": "", "source": "nexusfetch:ddg"}
        ]})

    hits = await _client(handler).search("acme funding", limit=3)

    assert seen["token"] == "t"
    assert hits[0]["url"] == "https://x.test/a"


async def test_a_blocked_engine_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "blocked"})

    client = _client(handler)
    assert await client.search("acme funding", limit=3) == []
    assert client.last_failure == "blocked"


async def test_an_unreachable_service_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _client(handler)
    assert await client.search("acme funding", limit=3) == []
    assert "unreachable" in client.last_failure


async def test_an_unconfigured_client_does_not_call_anything():
    client = FetchClient(base_url="", token="")
    assert await client.search("acme funding", limit=3) == []
    assert client.last_failure == "not configured"


async def test_fetch_page_returns_the_document():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "final_url": "https://acme.test/pricing",
                                         "html": "<html></html>", "text": "Pricing", "title": "Pricing"})

    page = await _client(handler).fetch_page("https://acme.test/pricing")
    assert page["text"] == "Pricing"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_fetch_client.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching.client'`

- [ ] **Step 3: Write the client**

```python
# nexus/fetching/client.py
"""Talks to nexus-fetch. Total: a failure is an empty answer plus a reason, never an exception.

Signal collection must degrade, not stop — the posture every provider seam in this codebase takes.
`last_failure` carries the reason so the crawl-history row can record `error` rather than `empty`.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("nexus.fetching.client")


class FetchClient:
    def __init__(self, *, base_url: str = "", token: str = "", timeout_s: float = 20.0, http=None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout_s = timeout_s
        self._http = http
        self.last_failure = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def _post(self, path: str, payload: dict) -> dict | None:
        if not self.configured:
            self.last_failure = "not configured"
            return None
        client = self._http or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s)
        try:
            response = await client.post(path, json=payload,
                                         headers={"X-Fetch-Token": self.token})
            if response.status_code == 503:
                self.last_failure = "blocked"
                return None
            if response.status_code >= 400:
                self.last_failure = f"service returned {response.status_code}"
                return None
            self.last_failure = ""
            return response.json()
        except Exception as exc:
            self.last_failure = f"unreachable: {type(exc).__name__}"
            logger.warning("nexus-fetch %s failed: %r", path, exc)
            return None
        finally:
            if self._http is None:
                await client.aclose()

    async def search(self, query: str, *, limit: int = 5, recency_days: int = 0) -> list[dict]:
        body = await self._post("/search", {"query": query, "limit": limit,
                                            "recency_days": recency_days})
        return list((body or {}).get("hits") or [])

    async def fetch_page(self, url: str, *, mode: str = "http") -> dict | None:
        return await self._post("/fetch", {"url": url, "mode": mode, "timeout_s": self.timeout_s})


_client: FetchClient | None = None


def get_fetch_client() -> FetchClient:
    """Process-wide client built from settings. Rebuilt when the URL or token changes."""
    global _client
    from nexus.core.config import get_settings

    settings = get_settings()
    url = (settings.fetch_service_url or "").strip()
    token = (settings.fetch_service_token or "").strip()
    if _client is None or _client.base_url != url.rstrip("/") or _client.token != token:
        _client = FetchClient(base_url=url, token=token,
                              timeout_s=float(settings.fetch_service_timeout_s))
    return _client


def set_fetch_client(client: FetchClient | None) -> None:
    """Test seam, mirroring `set_search_provider`."""
    global _client
    _client = client
```

- [ ] **Step 4: Add the settings**

In `nexus/core/config.py`, in the self-hosted fetching block added in Task 2:

```python
    #: Base URL of the nexus-fetch service, e.g. http://10.0.0.7:8081. Empty means the service is
    #: not used at all and every search goes to the configured paid provider, as it does today.
    fetch_service_url: str = ""
    #: Shared secret. The service refuses every call without it.
    fetch_service_token: str = ""
    fetch_service_timeout_s: float = 20.0
```

- [ ] **Step 5: Add the panel entry and forbid the token**

In `nexus/runtime_config/catalog.py`, add a `SettingSpec` for `fetch_service_url` (risk `high`,
placeholder `http://10.0.0.7:8081`) and one for `fetch_service_timeout_s`; add a URL validator for
`fetch_service_url` in `_VALIDATORS` beside `email_verify_url`'s; and add `fetch_service_token` to
`FORBIDDEN` with the reason: the panel returns values in plaintext, exactly as
`email_verify_auth_header` does.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_fetch_client.py tests/test_runtime_control_plane.py -v -n0`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add nexus/fetching/client.py nexus/core/config.py nexus/runtime_config/catalog.py tests/test_fetch_client.py
git commit -m "feat(fetching): client for nexus-fetch, token forbidden in the panel"
```

---

### Task 11: the self-hosted search provider

**Files:**
- Modify: `nexus/integrations/search/engines.py`
- Modify: `nexus/integrations/search/provider.py` (`build_search_provider`)
- Test: `tests/test_selfhosted_search_provider.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_selfhosted_search_provider.py
"""The self-hosted backend is a `plain` engine, like DuckDuckGo.

Declaring `operator` would make every dork render `site:` terms, which these endpoints match
literally — measured: zero results, silently, forever.
"""
from __future__ import annotations

from nexus.fetching.client import FetchClient
from nexus.integrations.search.engines import SelfHostedSearchProvider
from nexus.integrations.search.provider import build_search_provider


class FakeClient(FetchClient):
    def __init__(self, hits=None, failure=""):
        super().__init__(base_url="http://fetch.test", token="t")
        self._hits = hits or []
        self.last_failure = failure
        self.calls: list[dict] = []

    async def search(self, query, *, limit=5, recency_days=0):
        self.calls.append({"query": query, "limit": limit, "recency_days": recency_days})
        return list(self._hits)


def test_it_declares_the_plain_dialect():
    assert SelfHostedSearchProvider(client=FakeClient()).query_dialect == "plain"


async def test_hits_are_normalised_to_search_hits():
    client = FakeClient(hits=[{"title": "Acme raises $40M", "url": "https://x.test/a",
                              "snippet": "s", "source": "nexusfetch:ddg"}])

    hits = await SelfHostedSearchProvider(client=client).search("acme funding", limit=3)

    assert hits[0].title == "Acme raises $40M"
    assert hits[0].url == "https://x.test/a"
    assert hits[0].source == "nexusfetch:ddg"


async def test_recency_is_passed_through():
    client = FakeClient()
    await SelfHostedSearchProvider(client=client).search_recent("acme funding", limit=3, days=120)
    assert client.calls[0]["recency_days"] == 120


async def test_a_failure_is_reported_on_last_failure():
    provider = SelfHostedSearchProvider(client=FakeClient(failure="blocked"))
    assert await provider.search("acme funding", limit=3) == []
    assert provider.last_failure == "blocked"


def test_the_settings_token_builds_it():
    assert isinstance(build_search_provider("nexusfetch"), SelfHostedSearchProvider)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_selfhosted_search_provider.py -v -n0`
Expected: FAIL, `ImportError: cannot import name 'SelfHostedSearchProvider'`

- [ ] **Step 3: Write the provider**

In `nexus/integrations/search/engines.py`:

```python
class SelfHostedSearchProvider(SearchProvider):
    """Searches through our own nexus-fetch service instead of a paid API.

    `plain`, like DuckDuckGo, because that is what the service scrapes. Over-claiming the dialect
    costs every result: an `operator` dork on these endpoints matches nothing and does not error.
    """

    name = "nexusfetch"
    query_dialect = "plain"

    def __init__(self, client=None):
        from nexus.fetching.client import get_fetch_client

        self._client = client or get_fetch_client()

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return await self._hits(query, limit=limit, recency_days=0)

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90,
                            include_domains: tuple = (), exclude_domains: tuple = ()) -> list[SearchHit]:
        # The domains are already inside the query for a `plain` dialect, as the base class notes.
        return await self._hits(query, limit=limit, recency_days=days)

    async def _hits(self, query: str, *, limit: int, recency_days: int) -> list[SearchHit]:
        raw = await self._client.search(query, limit=limit, recency_days=recency_days)
        self.last_failure = self._client.last_failure
        return [
            SearchHit(title=h.get("title", ""), url=h.get("url", ""),
                      snippet=h.get("snippet", ""), source=h.get("source", self.name))
            for h in raw if h.get("url")
        ]
```

- [ ] **Step 4: Teach the factory the token**

In `nexus/integrations/search/provider.py`, inside `build_search_provider`, before the hosted-engine
branch:

```python
    if key in ("nexusfetch", "selfhosted"):
        from nexus.integrations.search.engines import SelfHostedSearchProvider

        return SelfHostedSearchProvider()
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_selfhosted_search_provider.py -v -n0`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add nexus/integrations/search/engines.py nexus/integrations/search/provider.py tests/test_selfhosted_search_provider.py
git commit -m "feat(search): self-hosted search provider over nexus-fetch"
```

---

### Task 12: the fallback chain

**Files:**
- Create: `nexus/integrations/search/fallback.py`
- Modify: `nexus/integrations/search/provider.py` (`signal_search_choice` usage in `DorkedSearchSource._provider` stays as is; the chain is built here)
- Test: `tests/test_search_fallback_chain.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_fallback_chain.py
"""Self-hosted first, paid second, keyless floor last — and never a lie about the dialect.

The chain must declare `plain`, the dialect of its WEAKEST member. A query rendered with `site:`
for Firecrawl returns nothing on DuckDuckGo, and the chain cannot know in advance which member will
answer.
"""
from __future__ import annotations

from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit


class Stub:
    def __init__(self, name, hits=None, failure=""):
        self.name = name
        self.query_dialect = "operator" if name == "firecrawl" else "plain"
        self.last_failure = failure
        self._hits = hits or []
        self.calls = 0

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.calls += 1
        return list(self._hits)

    async def search(self, query, *, limit=5):
        self.calls += 1
        return list(self._hits)


HIT = SearchHit(title="Acme raises $40M", url="https://x.test/a", snippet="", source="x")


def test_the_chain_declares_the_weakest_dialect():
    chain = FallbackSearchProvider([Stub("nexusfetch"), Stub("firecrawl")])
    assert chain.query_dialect == "plain"


async def test_the_first_provider_that_answers_wins():
    first, second = Stub("nexusfetch", hits=[HIT]), Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    await chain.search_recent("acme funding", limit=3, days=90)

    assert (first.calls, second.calls) == (1, 0)


async def test_a_blocked_provider_falls_through():
    first = Stub("nexusfetch", hits=[], failure="blocked")
    second = Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert [h.url for h in hits] == ["https://x.test/a"]
    assert second.calls == 1
    assert chain.answered_by == "firecrawl"


async def test_an_empty_answer_with_no_failure_is_accepted_not_retried():
    # "Nothing was published about this company" is a real answer. Retrying it down the chain would
    # pay a provider for a question we already had an answer to.
    first, second = Stub("nexusfetch", hits=[]), Stub("firecrawl", hits=[HIT])
    chain = FallbackSearchProvider([first, second])

    assert await chain.search_recent("acme funding", limit=3, days=90) == []
    assert second.calls == 0


async def test_when_every_provider_fails_the_reason_survives():
    chain = FallbackSearchProvider([
        Stub("nexusfetch", failure="blocked"), Stub("firecrawl", failure="no key")
    ])

    assert await chain.search_recent("acme funding", limit=3, days=90) == []
    assert chain.last_failure == "nexusfetch: blocked; firecrawl: no key"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_search_fallback_chain.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.integrations.search.fallback'`

- [ ] **Step 3: Write the chain**

```python
# nexus/integrations/search/fallback.py
"""Try the cheap backend first, fall through to the paid one, then to the keyless floor.

Collection degrades rather than stops — the posture every provider seam here takes.

Two rules that are easy to get wrong:

* **The chain declares the dialect of its WEAKEST member.** The dork renders its query BEFORE the
  search, and it cannot know which member will answer. An `operator` query returns zero results on
  a `plain` engine, silently; a `plain` query merely loses some precision on an operator engine.
* **An empty answer with no failure is an ANSWER.** Falling through on it would pay the next
  provider for a question already answered, and "nothing was published" is the common case.
"""
from __future__ import annotations

import logging

from nexus.integrations.search.provider import SearchHit, SearchProvider

logger = logging.getLogger("nexus.search.fallback")

_DIALECT_RANK = {"plain": 0, "operator": 1, "semantic": 2}


class FallbackSearchProvider(SearchProvider):
    name = "fallback"

    def __init__(self, providers: list) -> None:
        self.providers = [p for p in providers if p is not None]
        self.answered_by = ""

    @property
    def query_dialect(self) -> str:
        dialects = [getattr(p, "query_dialect", "plain") for p in self.providers]
        return min(dialects, key=lambda d: _DIALECT_RANK.get(d, 0)) if dialects else "plain"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return await self._run("search", query, limit=limit)

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90,
                            include_domains: tuple = (), exclude_domains: tuple = ()) -> list[SearchHit]:
        return await self._run("search_recent", query, limit=limit, days=days,
                               include_domains=include_domains, exclude_domains=exclude_domains)

    async def _run(self, method: str, query: str, **kwargs) -> list[SearchHit]:
        failures: list[str] = []
        for provider in self.providers:
            try:
                if method == "search_recent":
                    hits = await provider.search_recent(query, **kwargs)
                else:
                    hits = await provider.search(query, limit=kwargs.get("limit", 5))
            except TypeError:
                # An injected double or an older provider without the structured-domain kwargs,
                # handled the same way `DorkedSearchSource._run` handles it.
                hits = await provider.search(query, limit=kwargs.get("limit", 5))
            except Exception as exc:
                failures.append(f"{provider.name}: {type(exc).__name__}")
                continue
            failure = getattr(provider, "last_failure", "")
            if failure:
                failures.append(f"{provider.name}: {failure}")
                continue
            self.answered_by = provider.name
            self.last_failure = ""
            return list(hits or [])
        self.answered_by = ""
        self.last_failure = "; ".join(failures)
        return []
```

- [ ] **Step 4: Build the chain for signals**

In `nexus/integrations/search/provider.py`, add beside `signal_search_choice`:

```python
def build_signal_search_provider() -> SearchProvider:
    """What the signal pipeline searches with: self-hosted first, then the configured paid engine.

    The self-hosted member is present only when `fetch_service_url` is set, so a deployment that
    has not provisioned the VM behaves exactly as it does today.
    """
    from nexus.core.config import get_settings
    from nexus.integrations.search.fallback import FallbackSearchProvider

    chain = []
    if (get_settings().fetch_service_url or "").strip():
        chain.append(build_search_provider("nexusfetch"))
    chain.append(build_search_provider(signal_search_choice()))
    return FallbackSearchProvider(chain) if len(chain) > 1 else chain[0]
```

In `nexus/ingestion/sources.py`, `DorkedSearchSource._provider` builds the chain instead of a single
provider — replace `self._search = build_search_provider(choice)` with
`self._search = build_signal_search_provider()` and import it alongside the others. Keep the
`choice` comparison so the provider is still rebuilt when the setting moves.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_search_fallback_chain.py tests/test_signal_search_cache.py -v -n0`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nexus/integrations/search/fallback.py nexus/integrations/search/provider.py nexus/ingestion/sources.py tests/test_search_fallback_chain.py
git commit -m "feat(search): self-hosted -> paid -> keyless fallback chain for signals"
```

---

### Task 13: health row and boot warning

**Files:**
- Create: `nexus/fetching/health.py`
- Modify: `nexus/api/routers/admin_health.py`
- Test: `tests/test_fetch_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_health.py
"""An unreachable fetcher must be visible, because nothing else reports it.

When it cannot answer, signals silently fall back to the paid provider — the bill goes up and the
console says all clear. That is exactly what the email-verifier health row exists to prevent.
"""
from __future__ import annotations

import httpx

from nexus.fetching.client import FetchClient, set_fetch_client
from nexus.fetching.health import check_fetch_service


async def test_unconfigured_is_reported_as_unconfigured():
    set_fetch_client(FetchClient(base_url="", token=""))
    check = await check_fetch_service()
    assert check.status == "unconfigured"
    assert "paid provider" in check.detail


async def test_a_healthy_service_is_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "browser": True})

    set_fetch_client(FetchClient(
        base_url="http://fetch.test", token="t",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fetch.test"),
    ))
    check = await check_fetch_service()
    assert check.status == "ok"


async def test_an_unreachable_service_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    set_fetch_client(FetchClient(
        base_url="http://fetch.test", token="t",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fetch.test"),
    ))
    check = await check_fetch_service()
    assert check.status == "error"
    assert "unreachable" in check.detail
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_fetch_health.py -v -n0`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.fetching.health'`

- [ ] **Step 3: Write the check**

```python
# nexus/fetching/health.py
"""Is the self-hosted fetcher answering? Mirrors `nexus/verification/health.py`.

A dead fetcher fails soft — every search falls back to the paid provider — so the only symptom is a
bigger bill. This is the row that says so.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FetchCheck:
    status: str      # ok | unconfigured | error
    detail: str
    url: str = ""


async def check_fetch_service() -> FetchCheck:
    from nexus.fetching.client import get_fetch_client

    client = get_fetch_client()
    if not client.configured:
        return FetchCheck(
            "unconfigured",
            "no fetch service URL or token; every signal search goes to the paid provider",
        )
    payload = await client.health()
    if payload is None:
        return FetchCheck("error", client.last_failure or "unreachable", client.base_url)
    if not payload.get("ok"):
        return FetchCheck("error", "service reported not ok", client.base_url)
    browser = "browser tier available" if payload.get("browser") else "HTTP tier only"
    return FetchCheck("ok", browser, client.base_url)
```

The check calls `client.health()`, added in the next step. Reading the client's private `_http`
from here would put the transport in two places, and the first thing to drift would be the timeout.

- [ ] **Step 3b: Add `health()` to the client**

In `nexus/fetching/client.py`, beside `search` and `fetch_page`:

```python
    async def health(self) -> dict | None:
        """The service's own health payload, or ``None`` with ``last_failure`` set."""
        if not self.configured:
            self.last_failure = "not configured"
            return None
        client = self._http or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s)
        try:
            response = await client.get("/health")
            if response.status_code >= 400:
                self.last_failure = f"service returned {response.status_code}"
                return None
            self.last_failure = ""
            return response.json()
        except Exception as exc:
            self.last_failure = f"unreachable: {type(exc).__name__}"
            return None
        finally:
            if self._http is None:
                await client.aclose()
```

- [ ] **Step 4: Add the probe to Platform health**

In `nexus/api/routers/admin_health.py`, beside `_probe_email_verifier`:

```python
async def _probe_web_fetcher() -> tuple[str, str]:
    """Whether the self-hosted fetcher answers. When it does not, nothing fails — searches fall
    back to the paid provider, so the only symptom is the bill."""
    from nexus.fetching.health import check_fetch_service

    check = await check_fetch_service()
    status = {"ok": OK, "unconfigured": UNCONFIGURED}.get(check.status, ERROR)
    where = f" (url={check.url})" if check.url else ""
    return status, f"{check.detail}{where}"
```

and register it in the probe tuple:

```python
    ("web fetcher", _probe_web_fetcher),
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_fetch_health.py tests/test_admin_health.py -v -n0`
Expected: PASS. If `tests/test_admin_health.py` does not exist, run `pytest tests -k health -n0`.

- [ ] **Step 6: Commit**

```bash
git add nexus/fetching/health.py nexus/api/routers/admin_health.py tests/test_fetch_health.py
git commit -m "feat(fetching): web fetcher health row - a silent fallback is a visible bill"
```

---

### Task 14: fetch first-party pages through the service

**Files:**
- Modify: `nexus/ingestion/webwatch.py` (`_get`)
- Test: `tests/test_webwatch_via_fetch_service.py`

Website watch fetches a company's own pricing, security, careers and about pages with plain httpx
and a static User-Agent. Those are the pages most likely to be behind Cloudflare or rendered by
JavaScript, which is exactly what the stealth tier is for — and the same fetch is repeated for every
tenant tracking the company, so it belongs in the cache too.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webwatch_via_fetch_service.py
"""First-party pages go through nexus-fetch when it is configured, and through httpx when it is not.

The fallback is not optional: a deployment with no VM must keep watching pages exactly as it does
today.
"""
from __future__ import annotations

from nexus.fetching.client import FetchClient, set_fetch_client
from nexus.ingestion import webwatch


class FakeClient(FetchClient):
    def __init__(self, page=None):
        super().__init__(base_url="http://fetch.test", token="t")
        self._page = page
        self.urls: list[str] = []

    async def fetch_page(self, url, *, mode="http"):
        self.urls.append(url)
        return self._page


async def test_a_configured_service_serves_the_page():
    set_fetch_client(FakeClient(page={"status": 200, "final_url": "https://acme.test/pricing",
                                      "html": "<html>Plans</html>", "text": "Plans", "title": "Plans"}))

    status, body = await webwatch._get("https://acme.test/pricing")

    assert (status, body) == (200, "<html>Plans</html>")


async def test_an_unconfigured_service_falls_back_to_httpx(monkeypatch):
    set_fetch_client(FetchClient(base_url="", token=""))
    called = {}

    async def fake_fetch(url):
        called["url"] = url
        return 200, "<html>direct</html>"

    status, body = await webwatch._get("https://acme.test/pricing", fake_fetch)

    assert called["url"] == "https://acme.test/pricing"
    assert (status, body) == (200, "<html>direct</html>")


async def test_a_service_failure_falls_back_rather_than_losing_the_page():
    set_fetch_client(FakeClient(page=None))

    status, body = await webwatch._get("https://acme.test/pricing")

    # httpx ran and failed offline; the point is that it was TRIED, so status is a real outcome
    # rather than an exception escaping into the crawl.
    assert status in (0, 200)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_webwatch_via_fetch_service.py -v -n0`
Expected: FAIL on the first test — `_get` never consults the client.

- [ ] **Step 3: Route `_get` through the client when it is configured**

```python
async def _get(url: str, fetch=None) -> tuple[int, str]:
    """(status, body). Never raises.

    Prefers nexus-fetch when it is configured: these are the pages most likely to sit behind
    Cloudflare or to be rendered by JavaScript, which plain httpx cannot read. An unconfigured or
    failing service falls through to httpx, so a deployment without the VM behaves exactly as it
    did before.
    """
    if fetch is None:
        from nexus.fetching.client import get_fetch_client

        client = get_fetch_client()
        if client.configured:
            page = await client.fetch_page(url, mode="http")
            if page and page.get("html"):
                return int(page.get("status") or 200), str(page.get("html") or "")
    try:
        if fetch is not None:
            return await fetch(url)
        import httpx

        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            return resp.status_code, resp.text
    except Exception as exc:
        logger.warning("page fetch failed for %s: %r", url, exc)
        return 0, ""
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_webwatch_via_fetch_service.py -v -n0`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the website-watch suite for regressions**

Run: `pytest tests -k webwatch -n0`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nexus/ingestion/webwatch.py tests/test_webwatch_via_fetch_service.py
git commit -m "feat(signals): fetch first-party pages through nexus-fetch when it is available"
```

---

# Phase C — shadow, promote, document

### Task 15: shadow comparison

**Files:**
- Create: `scripts/fetch_shadow_report.py`
- Modify: `nexus/ingestion/sources.py` (record which member answered)
- Test: `tests/test_fetch_shadow_provenance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetch_shadow_provenance.py
"""Which backend answered has to be recorded, or the promotion decision is a guess.

`companies/diff.py` reads asymmetrically: results only the PAID provider found are the failure,
because those are what we would lose by promoting. The same read applies here, and it needs the
per-query record below.
"""
from __future__ import annotations

from nexus.ingestion.sources import DorkedSearchSource
from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit
from nexus.models.account import Account


class Stub:
    def __init__(self, name, hits=None, failure=""):
        self.name = name
        self.query_dialect = "plain"
        self.last_failure = failure
        self._hits = hits or []

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        return list(self._hits)


async def test_the_answering_backend_is_recorded_per_query():
    hit = SearchHit(title="Acme raises $40M", url="https://x.test/a", snippet="", source="x")
    chain = FallbackSearchProvider([Stub("nexusfetch", failure="blocked"), Stub("firecrawl", hits=[hit])])
    source = DorkedSearchSource(search=chain, max_queries=1)

    await source.fetch(Account(id="a1", tenant_id="t1", name="Acme", domain="acme.test"))

    assert source.last_provenance["queries"][0]["answered_by"] == "firecrawl"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_fetch_shadow_provenance.py -v -n0`
Expected: FAIL, `KeyError: 'answered_by'`

- [ ] **Step 3: Record the answering member**

In `DorkedSearchSource.fetch`, beside the `cached` line added in Task 5:

```python
            self.last_provenance["queries"][-1]["answered_by"] = getattr(
                self._provider(), "answered_by", getattr(self._provider(), "name", "")
            )
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_fetch_shadow_provenance.py -v -n0`
Expected: PASS

- [ ] **Step 4b: Write the failing test for shadow mode**

```python
# tests/test_search_shadow_mode.py
"""Shadow: ask both, use the PAID answer, record what the free one would have returned.

The fallback chain alone cannot catch the failure this exists for. A self-hosted engine that
answers with WORSE results never falls through — it just quietly degrades what reps see. Shadow is
how we find that out before promoting it.
"""
from __future__ import annotations

from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit


class Stub:
    def __init__(self, name, hits):
        self.name = name
        self.query_dialect = "plain"
        self.last_failure = ""
        self._hits = hits
        self.calls = 0

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.calls += 1
        return list(self._hits)


FREE = [SearchHit(title="A directory profile", url="https://dir.test/acme", snippet="", source="f")]
PAID = [SearchHit(title="Acme raises $40M", url="https://news.test/a", snippet="", source="p")]


async def test_shadow_uses_the_paid_answer_and_calls_both():
    free, paid = Stub("nexusfetch", FREE), Stub("firecrawl", PAID)
    chain = FallbackSearchProvider([free, paid], shadow=True)

    hits = await chain.search_recent("acme funding", limit=3, days=90)

    assert [h.url for h in hits] == ["https://news.test/a"]
    assert (free.calls, paid.calls) == (1, 1)
    assert chain.shadow_report == {"nexusfetch": 1, "firecrawl": 1}
```

- [ ] **Step 4c: Add shadow mode to the chain**

In `nexus/integrations/search/fallback.py`, take the flag in `__init__` and branch at the top of
`_run`:

```python
    def __init__(self, providers: list, *, shadow: bool = False) -> None:
        self.providers = [p for p in providers if p is not None]
        self.answered_by = ""
        self.shadow = shadow
        #: Hit counts per provider for the last shadow query, for the promotion decision.
        self.shadow_report: dict[str, int] = {}
```

```python
        if self.shadow and len(self.providers) > 1:
            # Ask everyone, count what each returned, and answer with the LAST member — the paid
            # one. Nothing about what a rep sees changes while this is on.
            self.shadow_report = {}
            answer: list = []
            for provider in self.providers:
                try:
                    if method == "search_recent":
                        hits = await provider.search_recent(query, **kwargs)
                    else:
                        hits = await provider.search(query, limit=kwargs.get("limit", 5))
                except Exception:
                    hits = []
                self.shadow_report[provider.name] = len(hits or [])
                if not getattr(provider, "last_failure", ""):
                    answer = list(hits or [])
                    self.answered_by = provider.name
            return answer
```

Build it from a setting in `build_signal_search_provider`:

```python
    return (FallbackSearchProvider(chain, shadow=bool(get_settings().signal_fetch_shadow))
            if len(chain) > 1 else chain[0])
```

Add the setting beside the other fetching settings in `nexus/core/config.py`:

```python
    #: Ask the self-hosted backend alongside the paid one and record both, but ANSWER with the paid
    #: result. The only way to see whether promoting it would cost recall, before it does.
    signal_fetch_shadow: bool = False
```

and a `SettingSpec` for it in the signals group of `nexus/runtime_config/catalog.py` (risk `low`;
effect: "asks both backends and keeps the paid answer, for comparison"; warning: "doubles signal
search volume while it is on, so leave it on only as long as the comparison needs").

- [ ] **Step 4d: Run the shadow tests**

Run: `pytest tests/test_search_shadow_mode.py tests/test_search_fallback_chain.py -v -n0`
Expected: PASS

- [ ] **Step 5: Write the report script**

```python
# scripts/fetch_shadow_report.py
"""Who answered the signal searches, and what did it cost?

Reports, never repairs — like `nexus/billing/reconcile.py` and `nexus/companies/diff.py`. Read it
asymmetrically: a query only the PAID provider could answer is what promoting the self-hosted
backend would lose. Cache hits are the saving.

Usage:
    python scripts/fetch_shadow_report.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.source_run import SignalSourceRun


async def main(days: int) -> None:
    since = utcnow() - timedelta(days=days)
    answered, cached, failed = Counter(), 0, 0
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(SignalSourceRun).where(SignalSourceRun.started_at >= since,
                                          SignalSourceRun.source == "dork")
        )).scalars().all()
        for row in rows:
            for query in (row.provenance or {}).get("queries", []):
                if query.get("cached"):
                    cached += 1
                elif query.get("failed"):
                    failed += 1
                else:
                    answered[query.get("answered_by") or "unknown"] += 1

    total = sum(answered.values()) + cached + failed
    print(f"dork queries in the last {days} days: {total}")
    print(f"  served from cache : {cached}  ({_pct(cached, total)})  <- bought nothing")
    for name, count in answered.most_common():
        print(f"  answered by {name:<12}: {count}  ({_pct(count, total)})")
    print(f"  failed            : {failed}  ({_pct(failed, total)})")
    paid = sum(c for n, c in answered.items() if n not in ("nexusfetch", "unknown"))
    print(f"\nstill paid for: {paid} queries ~= ${paid * 0.0064:.2f} at the Firecrawl rate")


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    asyncio.run(main(parser.parse_args().days))
```

`SignalSourceRun` is tenant-scoped, so this cross-tenant read MUST use `get_platform_sessionmaker()`
— under the app's RLS-bound role it would return zero rows and print a platform nobody uses, which
is the trap CLAUDE.md documents and this codebase has walked into three times. Run it read-only
against staging first.

- [ ] **Step 6: Commit**

```bash
git add nexus/ingestion/sources.py nexus/integrations/search/fallback.py nexus/integrations/search/provider.py \
        nexus/core/config.py nexus/runtime_config/catalog.py scripts/fetch_shadow_report.py \
        tests/test_fetch_shadow_provenance.py tests/test_search_shadow_mode.py
git commit -m "feat(signals): shadow mode and a report on which backend answered"
```

---

### Task 16: documentation and the operator runbook

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.env.example`
- Modify: `deploy/.env.production.example`
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Document the subsystem in `CLAUDE.md`**

Add a section after *Signal sources*, in the file's established voice — what it does, and the
decisions that are load-bearing:

```markdown
## Self-hosted fetching (`nexus/fetching/`, `services/fetch/`)

Signal collection re-asked the same four dork queries about the same company every six hours, per
tenant, at $0.0064 each: ~$3/month per hot account, bought again for every workspace tracking it.
`DataSourceRegistry`'s cache is in-process and `DorkedSearchSource` bypassed it entirely.

- **`web_cache` is platform-global (migration `0057`, no `tenant_id`)**, like `companies` and
  `people`. Enrolling it in RLS would return zero rows to the shared reader — silently.
- **The TTL IS the cadence.** Funding and news 6h, everything else searched 24h. No scheduler
  change: a fresh cached answer short-circuits before the request, so `next_refresh_at` and
  `tiering.classify` are untouched.
- **A failed search is never cached.** A condemned key pool returns `[]` with `last_failure` set,
  not an exception; caching that freezes an outage in place for the whole TTL and the account reads
  "no signals" long after the key is fixed.
- **The chain declares `plain`, the dialect of its weakest member.** The dork renders before the
  search and cannot know who will answer; an `operator` query returns zero results on DuckDuckGo
  and does not error.
- **`nexus-fetch` is stateless and never publicly reachable.** Shared secret, firewall to the app's
  egress, and `nexus/fetching/guard.py` refusing private, loopback, link-local and metadata hosts —
  an open fetch endpoint is an open proxy, the argument `nexus/sources/safety.py` makes about DSNs.
  It runs on its own VM, NOT the Reacher box: scraping egress must not share an IP with email
  verification.
- **`blocked` is not `empty`.** A refusal answers 503 and sets `last_failure`, so a crawl records
  `error` rather than a quiet market.
- **Signal scans are still metered nowhere.** `signal.news_scan` and its siblings are priced in
  `rates.py` with no call site, so this saving is pure COGS. Adding metering would start charging
  customers for something they have never been charged for — a pricing decision, not a side effect.
```

- [ ] **Step 2: Document the settings in both env examples**

```bash
# --- Self-hosted fetching -----------------------------------------------------------------
# nexus-fetch scrapes tolerant search endpoints and fetches pages, so repeated signal searches
# stop being bought. Empty = not used; every search goes to the paid provider, as before.
NEXUS_FETCH_SERVICE_URL=
NEXUS_FETCH_SERVICE_TOKEN=
NEXUS_FETCH_SERVICE_TIMEOUT_S=20
# Reuse answers instead of re-buying them. The TTL is the re-ask cadence.
NEXUS_WEB_CACHE_ENABLED=true
NEXUS_SIGNAL_CACHE_TTL_FAST_S=21600
NEXUS_SIGNAL_CACHE_TTL_SLOW_S=86400
NEXUS_PAGE_CACHE_TTL_S=86400
```

- [ ] **Step 3: Add the row to `docs/ARCHITECTURE.md`**

Add `| Fetching | nexus/fetching + services/fetch | shared web cache; self-hosted SERP and page
fetching, paid fallback |` to the module table.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md .env.example deploy/.env.production.example docs/ARCHITECTURE.md
git commit -m "docs(fetching): self-hosted fetching, the cache and its cadence"
```

---

## Rollout after the code lands

These are operator steps, not code. Run them in order; each answers a question the next one needs.

1. **Ship Phase A only.** `NEXUS_FETCH_SERVICE_URL` stays empty, so the paid provider is still
   primary. Nothing about recall changes.
2. **After a week, measure.** `python scripts/fetch_shadow_report.py --days 7` against staging and
   production. The cache percentage is the saving Phase A bought; the paid count times $0.0064 is
   what remains to attack.
3. **Provision the VM** (`services/fetch/README.md`), set `NEXUS_FETCH_SERVICE_URL` and
   `NEXUS_FETCH_SERVICE_TOKEN` on the API and worker, and confirm the `web fetcher` row on Platform
   health reads ok.
4. **Turn on shadow** (`NEXUS_SIGNAL_FETCH_SHADOW=true`) for a few days on staging, then on
   production if you want production traffic in the comparison. Reps see the paid answer throughout.
   Read `scripts/fetch_shadow_report.py`: if the self-hosted backend returns materially fewer hits
   on funding dorks, it is not ready to be promoted, whatever it costs. Turn shadow off afterwards —
   it doubles search volume while it is on.
5. **Read the report again after a week of live use.** Queries answered by `nexusfetch` are free;
   anything the chain had to pay for tells you which dorks the self-hosted engines cannot serve.
6. **Record the new cost** through `PUT /admin/billing/costs/signal.news_scan` — a cost is an
   observation, and the margin floor validates against it. Read the work list it returns.
7. **Only then** consider lowering `signal_dork_max_queries` back up or widening coverage; the
   numbers, not the estimate, decide.

## Not in this plan, on purpose

- **Metering signal scans.** See the note at the top: it is a pricing decision.
- **Enrichment, research briefs and ICP-from-website** (the Exa searches) — the second sub-project.
- **Discovery and lookalikes, and the Apify actors** — the third, and the one that needs an index.
- **Residential proxies and Google/Bing scraping** — excluded by decision in the spec.
