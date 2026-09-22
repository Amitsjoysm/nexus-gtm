"""Durable, cross-tenant cache for searches and page fetches.

Every entry point is TOTAL. A cache exists to make collection cheaper, so it must never be able to
make collection fail: an unreachable database, a serialisation error or a schema surprise all
resolve to "miss", and the caller buys the answer as it does today.

Platform-global storage, so a company crawled for one tenant is not re-bought for the next. See
`nexus/models/web_cache.py` for why there is no `tenant_id`.

**Ordering matters for callers that also hold a tenant session.** `get()` and `put()` each open
their own platform session, separate from any tenant session the caller may have open. Complete
cache reads and writes *before* opening a tenant write transaction, the same discipline
`nexus/people/enrich.py` documents for its own platform-session lookup: nesting a second
connection's write inside an open tenant transaction is something Postgres tolerates and SQLite
deadlocks on. Even where Postgres does not deadlock outright, a hot key here can serialise other
callers on SQLite's single-writer lock, or queue up row locks on Postgres, while the tenant
transaction sits open and holding its own locks — a "works in production, hangs the test suite"
split, or a production stall under load, that is not worth the elegance of interleaving the two.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.web_cache import WebCache

logger = logging.getLogger("nexus.fetching.cache")


def cache_key(kind: str, subject: str, *, engine: str = "", limit: int = 0) -> str:
    """Deterministic id, so two workers racing on one query write the same row.

    A search query is case-insensitive, so it is lowercased; a page URL is not — `/Pricing` and
    `/pricing` can be different pages — so a page's subject keeps its case.
    """
    kind_norm = (kind or "").strip().lower()
    subject_norm = (subject or "").strip()
    if kind_norm != "page":
        subject_norm = subject_norm.lower()
    raw = "|".join(
        [kind_norm, subject_norm, (engine or "").strip().lower(), str(int(limit or 0))]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enabled() -> bool:
    from nexus.core.config import get_settings

    return bool(get_settings().web_cache_enabled)


async def get(kind: str, subject: str, *, engine: str = "", limit: int = 0):
    """The cached payload, or ``None`` when there is nothing fresh to serve."""
    key = "?"
    try:
        if not enabled():
            return None
        key = cache_key(kind, subject, engine=engine, limit=limit)
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
    key = "?"
    try:
        if not enabled():
            return
        key = cache_key(kind, subject, engine=engine, limit=limit)
        now = utcnow()
        try:
            size = len(json.dumps(payload).encode("utf-8"))
        except Exception:
            size = 0
        # Truncated, not validated, against the column width: an unknown or oversized `kind`
        # must still be a cache row, never a DataError that the blanket handler below would log
        # as an outage.
        kind_norm = (kind or "")[:16]
        async with get_platform_sessionmaker()() as session:
            row = await session.get(WebCache, key)
            if row is None:
                row = WebCache(id=key, kind=kind_norm, subject=(subject or "")[:2000])
                session.add(row)
            row.engine = (engine or "")[:64]
            row.payload = payload
            row.fetched_at = now
            row.expires_at = now + timedelta(seconds=float(ttl_s))
            row.bytes = size
            await session.commit()
    except IntegrityError:
        # Two workers raced on the same deterministic key; the loser's insert lost the primary
        # key. Normal operation, not an incident — the key exists precisely so this is harmless.
        logger.debug("web cache write lost the race for %s (another worker stored it first)", key)
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
