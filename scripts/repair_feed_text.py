#!/usr/bin/env python
"""Decode HTML entities and strip markup from signals that were stored before the parser cleaned them.

`clean_feed_text` fixes every signal ingested from now on. It does nothing for the ones already in
the table, and those are what a rep is looking at: measured on the live database, **73 of 139 RSS
signals carried raw entity codes and 74 carried HTML tags** — `&#8211;` where a dash belonged,
`&#8217;s` for an apostrophe, WordPress's `[&#8230;]` read-more marker.

Safe to run repeatedly. Cleaning is idempotent: text with no entities and no tags comes back
unchanged, so a second pass is a no-op.

**Run it where the database is.** In a containerised deployment the app's DSN lives in the
container's environment; a shell on the host resolves `NEXUS_DATABASE_URL` to its local default
instead, which is a stale SQLite file. That is not hypothetical — the first run of this script did
exactly that and died on `no such column: signal_events.subtype`, because the local file predated
migration 0043. It printed nothing about which database it had opened, so "it errored" and "it
repaired nothing" looked the same. Hence the banner below: the target is stated before any work.

    docker cp scripts/repair_feed_text.py nexus-gtm-app-1:/tmp/repair.py
    docker exec nexus-gtm-app-1 python /tmp/repair.py --dry-run   # report only
    docker exec nexus-gtm-app-1 python /tmp/repair.py             # apply

Only `title` and `body` are touched. `url` is left exactly as published — a URL is not display
text, and stripping anything that looks like a tag out of a query string would corrupt it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _redacted_dsn(settings) -> str:
    """The database this run will modify, with any password removed.

    Shown rather than logged: the operator needs to see "postgresql+asyncpg://...@postgres/nexus"
    and not "sqlite+aiosqlite:///./nexus.db" BEFORE the work starts, not after it fails.
    """
    import re

    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--limit", type=int, default=0, help="cap rows examined (0 = all)")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.sources import clean_feed_text
    from nexus.models.signal import SignalEvent

    # State the target before touching anything. A repair script that opens a different database
    # than the operator expects and says nothing is the worst kind: it reports success on an empty
    # table, or dies on a schema mismatch, and neither outcome names the cause.
    print(f"target: {_redacted_dsn(get_settings())}")
    if not args.dry_run:
        print("mode:   APPLY (writes)")
    else:
        print("mode:   dry run (no writes)")
    print()

    # Platform sessionmaker: this sweeps every tenant, and a tenant-bound session would silently
    # return zero rows for all but one of them.
    changed = 0
    examined = 0
    samples: list[tuple[str, str]] = []

    async with get_platform_sessionmaker()() as session:
        stmt = select(SignalEvent)
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = (await session.scalars(stmt)).all()

        for row in rows:
            examined += 1
            new_title = clean_feed_text(row.title) if row.title else row.title
            new_body = clean_feed_text(row.body) if row.body else row.body
            if new_title == row.title and new_body == row.body:
                continue
            if len(samples) < 8 and new_title != row.title:
                samples.append(((row.title or "")[:70], (new_title or "")[:70]))
            if not args.dry_run:
                row.title = new_title
                row.body = new_body
            changed += 1

        if not args.dry_run:
            await session.commit()

    verb = "would change" if args.dry_run else "changed"
    print(f"examined {examined} signals; {verb} {changed}")
    if samples:
        print("\nsample title rewrites:")
        for before, after in samples:
            print(f"  - {before}")
            print(f"  + {after}")
    if args.dry_run and changed:
        print("\nRe-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
