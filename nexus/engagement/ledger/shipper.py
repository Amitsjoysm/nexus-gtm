"""Outbox → archive (spec §18.2).

The archive is written FIRST and is the only store the shipper touches; training and insights are
built from it, so both are rebuildable and neither can drift from what was actually recorded.

What the shipper adds to each envelope before sealing it is the ``resolved`` block — who and what the
event was about, read from the app database at shipping time. Resolving here rather than at build
time is what makes the archive self-contained: a contact deleted next week does not take the meaning
of last week's events with it.

Failure posture, and it is deliberately not the job-retry machinery alone:

* Each row carries ``attempts`` and ``last_error``, and a row that failed waits ``2^attempts``
  minutes (capped at an hour) before it is tried again. So a store outage costs one failed batch per
  backoff window rather than one per heartbeat tick, and the jobs that ride in between find nothing
  due and return quietly instead of dead-lettering every minute.
* The handler still RAISES when a batch fails, so `workers/durability.py` retries and finally
  dead-letters — the evidence an operator needs, once per window instead of sixty times.
* Nothing is ever deleted before it is acknowledged: ``shipped_archive_at`` is set only after the
  insert commits, and retention removes rows seven days after that.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.ledger.shipper")

#: Rows per batch. A ledger event is at most 256 KB (`envelope.MAX_PAYLOAD_BYTES`), so a full batch
#: is bounded at ~125 MB in the worst case and a few MB in practice.
BATCH = 250
#: Batches per run. The heartbeat comes round again; an unbounded loop would starve every other job.
MAX_BATCHES = 8
#: How long a shipped row stays in the outbox. Long enough to re-ship after a store is restored from
#: a backup, short enough that the table stays small (spec §18.2).
RETENTION_DAYS = 7
_MAX_BACKOFF_MINUTES = 60


def retry_delay_minutes(attempts: int) -> int:
    """``0, 2, 4, 8, ... 60``. Pure, so the schedule is testable without a store."""
    if attempts <= 0:
        return 0
    return min(2 ** attempts, _MAX_BACKOFF_MINUTES)


def is_due(attempts: int, updated_at: datetime, now: datetime) -> bool:
    return updated_at + timedelta(minutes=retry_delay_minutes(attempts)) <= now


def archive_document(envelope: dict, resolved: dict) -> dict:
    """What is sealed: the envelope as written, plus who it was about at the time."""
    return {"envelope": envelope, "resolved": resolved}


def archive_row(secret: str, envelope: dict, resolved: dict) -> tuple:
    """One row for ``nexus_ledger.events``. Pure given the secret."""
    from nexus.engagement.ledger.pseudonym import archive_fernet, person_key

    document = archive_document(envelope, resolved)
    sealed = archive_fernet(secret).encrypt(
        json.dumps(document, default=str).encode("utf-8")).decode("ascii")
    emails = [e for e in (resolved.get("person_emails") or []) if e]
    keys = sorted({person_key(secret, email) for email in emails})
    return (
        envelope["event_id"],
        envelope["event_type"],
        int(envelope.get("schema_version") or 1),
        as_datetime(envelope["occurred_at"]),
        str(envelope.get("tenant_id") or ""),
        sealed,
        keys,
    )


def open_document(secret: str, sealed: str) -> dict:
    """The inverse, for the builders and the export script."""
    from nexus.engagement.ledger.pseudonym import archive_fernet

    return json.loads(archive_fernet(secret).decrypt(sealed.encode("ascii")).decode("utf-8"))


def as_datetime(value) -> datetime:
    """An envelope's ISO timestamp as a real datetime. asyncpg binds by inferred type before any
    cast in the SQL, so a string here is a DataError, not a coercion."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _claim(session, now: datetime, limit: int) -> list:
    """The oldest unshipped rows whose backoff has elapsed, across every workspace.

    Reads through the platform sessionmaker: under the RLS-bound app role a cross-tenant read
    returns ZERO ROWS rather than raising, which would read as "nothing to ship" forever.
    """
    from sqlalchemy import select

    from nexus.models.ledger import LedgerOutbox

    rows = (await session.execute(
        select(LedgerOutbox)
        .where(LedgerOutbox.shipped_archive_at.is_(None))
        .order_by(LedgerOutbox.occurred_at, LedgerOutbox.created_at)
        .limit(limit * 3)
    )).scalars().all()
    return [row for row in rows if is_due(row.attempts, row.updated_at, now)][:limit]


async def ship_batch(limit: int = BATCH) -> dict:
    """Ship one batch. Returns a summary; raises ``StoreUnavailable`` when the archive refused."""
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.ledger import resolve as resolver
    from nexus.engagement.ledger.stores import StoreUnavailable, connect

    secret = await config.pseudonym_secret()
    if not secret:
        return {"shipped": 0, "skipped": "the pseudonymisation secret is not configured"}

    now = utcnow()
    sessionmaker = get_platform_sessionmaker()
    async with sessionmaker() as session:
        rows = await _claim(session, now, limit)
        if not rows:
            return {"shipped": 0}
        prepared = []
        for row in rows:
            envelope = row.payload or {}
            resolved = await resolver.resolve(session, envelope)
            prepared.append((row, archive_row(secret, envelope, resolved.as_dict())))
        try:
            async with connect("archive") as conn:
                await conn.executemany(
                    "INSERT INTO nexus_ledger.events "
                    "(event_id, event_type, schema_version, occurred_at, tenant_id, sealed, "
                    " person_keys) VALUES ($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    [values for _row, values in prepared],
                )
        except Exception as exc:
            for row, _values in prepared:
                row.attempts += 1
                row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            await session.commit()
            logger.warning("ledger archive batch of %s failed", len(prepared), exc_info=True)
            raise StoreUnavailable(f"archive: {type(exc).__name__}") from exc
        for row, _values in prepared:
            row.shipped_archive_at = now
            row.last_error = None
        await session.commit()
    return {"shipped": len(prepared)}


async def ship(max_batches: int = MAX_BATCHES) -> dict:
    total = 0
    for _ in range(max_batches):
        result = await ship_batch()
        if result.get("skipped"):
            return result
        shipped = result["shipped"]
        total += shipped
        if shipped < BATCH:
            break
    return {"shipped": total}


async def purge_shipped() -> int:
    """Delete rows archived more than ``RETENTION_DAYS`` ago. Bounded, indexed, idempotent."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerOutbox

    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    async with get_platform_sessionmaker()() as session:
        result = await session.execute(
            delete(LedgerOutbox)
            .where(LedgerOutbox.shipped_archive_at.is_not(None))
            .where(LedgerOutbox.shipped_archive_at < cutoff)
        )
        await session.commit()
    return int(result.rowcount or 0)


async def backlog() -> dict:
    """What the Ledger tab and the health row report: how much is waiting, and how old it is."""
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.ledger import LedgerOutbox

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(
            select(func.count(LedgerOutbox.id), func.min(LedgerOutbox.occurred_at),
                   func.max(LedgerOutbox.attempts))
            .where(LedgerOutbox.shipped_archive_at.is_(None))
        )).one()
        waiting, oldest, attempts = int(row[0] or 0), row[1], int(row[2] or 0)
        errored = (await session.execute(
            select(func.count(LedgerOutbox.id))
            .where(LedgerOutbox.shipped_archive_at.is_(None))
            .where(LedgerOutbox.attempts > 0)
        )).scalar_one()
    age_s = int((utcnow() - oldest).total_seconds()) if oldest is not None else 0
    return {"waiting": waiting, "oldest_age_s": age_s, "retrying": int(errored or 0),
            "max_attempts": attempts}
