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

from sqlalchemy import delete

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
