#!/usr/bin/env python
"""Decode HTML entities and strip markup from signals that were stored before the parser cleaned them.

`clean_feed_text` fixes every signal ingested from now on. It does nothing for the ones already in
the table, and those are what a rep is looking at: measured on the live database, **73 of 139 RSS
signals carried raw entity codes and 74 carried HTML tags** — `&#8211;` where a dash belonged,
`&#8217;s` for an apostrophe, WordPress's `[&#8230;]` read-more marker.

Safe to run repeatedly. Cleaning is idempotent: text with no entities and no tags comes back
unchanged, so a second pass is a no-op.

    python scripts/repair_feed_text.py --dry-run     # show what would change, touch nothing
    python scripts/repair_feed_text.py               # apply

Only `title` and `body` are touched. `url` is left exactly as published — a URL is not display
text, and stripping anything that looks like a tag out of a query string would corrupt it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--limit", type=int, default=0, help="cap rows examined (0 = all)")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.sources import clean_feed_text
    from nexus.models.signal import SignalEvent

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
