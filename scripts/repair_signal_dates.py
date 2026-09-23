"""Re-date signals stored before sources passed their own dates.

Until 2026-09-23 no source set `occurred_at`, so every stored signal is dated the moment it was
collected — measured locally, 1,099 of 1,112 within ten minutes of collection. The day filter
compares against that date, so fixing the sources alone would leave the filter wrong until the old
rows aged out.

This re-dates only what a row can PROVE, and only rows not already dated by their source:

* a date in the stored URL (`/2025/03/04/`, `2025-03-04`) — no network;
* the filing date inside an SEC key (`sec:<anchor>:<form>:<YYYY-MM-DD>`) — no network;
* with ``--feeds``, the post's own date from the company's feed, matched by link. Opt-in because it
  fetches every tracked company's feed.

Everything else keeps its collection date and its "found" label, which is the truth about it.
Cross-tenant, so it runs on the platform sessionmaker: under the RLS-bound role this would see zero
rows and report a clean estate it never looked at.

Dry run by default — it reports and writes nothing:

    python scripts/repair_signal_dates.py
    python scripts/repair_signal_dates.py --feeds
    python scripts/repair_signal_dates.py --feeds --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import or_, select


async def _feed_dates(accounts, fetch) -> dict[str, tuple]:
    """``{post link: (occurred_at, dated)}`` read the way a live crawl reads the feed."""
    from nexus.ingestion.sources import RssSignalSource

    source = RssSignalSource(fetch=fetch, max_items=100)
    out: dict[str, tuple] = {}
    for account in accounts:
        try:
            for raw in await source.fetch(account):
                if raw.url and raw.dated == "event":
                    out[raw.url.strip()] = (raw.occurred_at, raw.dated)
        except Exception:  # one unreachable feed must not stop the repair
            continue
    return out


async def repair(*, apply: bool = False, feeds: bool = False, session=None, rss_fetch=None) -> dict:
    """Re-date what can be proven. Returns counts; writes only when ``apply``."""
    from nexus.core.dates import date_from_url, parse_when
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    async def run(s) -> dict:
        rows = (await s.execute(
            select(SignalEvent).where(
                or_(SignalEvent.dated.is_(None), SignalEvent.dated == "found")
            )
        )).scalars().all()

        by_feed: dict[str, tuple] = {}
        if feeds:
            ids = {r.account_id for r in rows if r.source == "rss" and r.account_id}
            accounts = (await s.execute(select(Account).where(Account.id.in_(ids)))).scalars().all()
            by_feed = await _feed_dates(accounts, rss_fetch)

        counts: Counter[str] = Counter()
        for row in rows:
            when, how = None, ""
            if row.source == "rss" and row.url and row.url.strip() in by_feed:
                when, how = by_feed[row.url.strip()][0], "from_feed"
            elif (row.dedupe_key or "").startswith("sec:"):
                when, how = parse_when((row.dedupe_key or "").rsplit(":", 1)[-1]), "from_sec"
            if when is None:
                when, how = date_from_url(row.url), "from_url"
            if when is None:
                counts["undatable"] += 1
                continue
            counts[how] += 1
            if apply:
                row.occurred_at = when
                row.dated = "event"
        if apply:
            # A caller's session is theirs to commit; our own is committed here.
            if session is None:
                await s.commit()
            else:
                await s.flush()
        return {"scanned": len(rows), "from_url": 0, "from_sec": 0, "from_feed": 0,
                "undatable": 0, **counts}

    if session is not None:
        return await run(session)
    async with get_platform_sessionmaker()() as own:
        return await run(own)


async def main(apply: bool, feeds: bool) -> None:
    result = await repair(apply=apply, feeds=feeds)
    verb = "Re-dated" if apply else "Would re-date"
    print(f"Scanned {result['scanned']} signals dated only by when they were found.")
    print(f"  {verb} {result['from_url']} from a date in their URL")
    print(f"  {verb} {result['from_sec']} from their SEC filing date")
    if feeds:
        print(f"  {verb} {result['from_feed']} from the post date in the company feed")
    print(f"  Left {result['undatable']} labelled 'found': nothing proves when they happened")
    if not apply:
        print("\nDry run: nothing was written. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--feeds", action="store_true", help="also re-read company feeds (fetches)")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.feeds))
