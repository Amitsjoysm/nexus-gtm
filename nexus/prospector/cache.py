"""The shared Prospector page cache (decision D10): platform-global, sealed, 24 hours.

An InfoJoy Search page is the same answer for every workspace that asks the same question, and the
provider bills per record returned, so a page is bought once and served to everyone for a day.

* **Sealed.** The rows carry the provider's PII (emails, phones), so the payload is Fernet-sealed
  under a key derived from ``secret_key`` with its own domain label; a row read elsewhere is
  ciphertext. A row that will not unseal (the key rotated, the row was altered) is a miss.
* **Expiring.** ``expires_at`` is indexed and ``prune`` (enqueued every heartbeat tick) deletes
  what is past it, so PII sits here 24 hours at most.
* **Total.** Every entry point catches everything and degrades to "miss": a cache must make the
  product cheaper and never make it fail.
* **Key = what was asked, never who asked.** sha256 of view + canonical filters + page + size; no
  tenant id, no user id. Two workers racing on one query write the same row and one insert loses.

Callers finish cache work before writing in a tenant transaction, the ordering
``nexus/fetching/cache.py`` documents: a second connection's write nested inside an open tenant
write transaction deadlocks SQLite's single writer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.prospector import ProspectorPage
from nexus.prospector.records import ParsedPage

logger = logging.getLogger("nexus.prospector.cache")

TTL = timedelta(hours=24)


def page_key(view: str, payload: dict, page: int, size: int) -> str:
    raw = json.dumps({"v": view, "f": payload, "p": int(page), "s": int(size)},
                     sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key() -> str:
    from nexus.core.config import get_settings

    digest = hashlib.sha256(f"prospector-cache|{get_settings().secret_key}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def _seal(parsed: ParsedPage) -> str:
    from nexus.core.crypto import seal_text

    return seal_text(json.dumps({"items": parsed.items, "total": parsed.total}), key=_key())


def _unseal(sealed: str) -> ParsedPage | None:
    from nexus.core.crypto import unseal_text

    text = unseal_text(sealed, key=_key())
    if not text:
        return None
    body = json.loads(text)
    items = body.get("items")
    if not isinstance(items, list):
        return None
    total = body.get("total")
    return ParsedPage(items=items, total=total if isinstance(total, int) else None)


async def get_page(key: str) -> ParsedPage | None:
    try:
        async with get_platform_sessionmaker()() as session:
            row = await session.get(ProspectorPage, key)
            if row is None or row.expires_at <= utcnow():
                return None
            return _unseal(row.payload_sealed)
    except Exception:
        logger.warning("prospector cache read failed for %s", key[:12], exc_info=True)
        return None


async def put_page(key: str, *, view: str, page: int, size: int, parsed: ParsedPage) -> None:
    try:
        now = utcnow()
        async with get_platform_sessionmaker()() as session:
            row = await session.get(ProspectorPage, key)
            if row is None:
                row = ProspectorPage(id=key)
                session.add(row)
            row.kind = (view or "")[:16]
            row.page_number = int(page)
            row.page_size = int(size)
            row.payload_sealed = _seal(parsed)
            row.row_count = len(parsed.items)
            row.total_count = parsed.total
            row.fetched_at = now
            row.expires_at = now + TTL
            await session.commit()
    except IntegrityError:
        logger.debug("prospector cache write lost the race for %s", key[:12])
    except Exception:
        logger.warning("prospector cache write failed for %s", key[:12], exc_info=True)


async def prune() -> int:
    """Delete expired pages. Returns how many went. Never raises."""
    try:
        async with get_platform_sessionmaker()() as session:
            result = await session.execute(
                delete(ProspectorPage).where(ProspectorPage.expires_at <= utcnow()))
            await session.commit()
            return int(result.rowcount or 0)
    except Exception:
        logger.warning("prospector cache prune failed", exc_info=True)
        return 0
