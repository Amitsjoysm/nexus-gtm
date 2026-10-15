"""Re-apply today's funding rules to funding signals already stored.

The rules changed on 2026-10-15 (`nexus/ingestion/sources.py`): needles are whole words, "raises"
needs an amount or a round named, "valuation" and "venture" need a round, RSS is classified on its
title alone, and web news no longer stores directory or profile pages. That fixes every signal
collected from now on and nothing already in the table. Measured on the local deploy: of 70 funding
signals stored in 30 days, 48 were not rounds ("toe raises", "raises outlook", "series creator",
a person's LinkedIn profile), and funding is the class that creates Inbox tasks and fires plays.

Only `kind` and `strength` change, to what the classifier now says (a profile page becomes news at
0.4). Nothing is deleted, and the title, body and url are untouched. Inbox tasks the old signals
already raised are left alone: a rep may have acted on one.

Dry run by default: it prints what it would change and writes nothing.

    python scripts/reclassify_funding_signals.py
    python scripts/reclassify_funding_signals.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select

#: The sources whose funding kind comes from the headline classifier. A trusted-kind dork never
#: produces `funding` (only job postings and tech installs are trusted), so `dork` is safe here.
SOURCES = ("rss", "web_news", "dork")


def reclassified(source: str, title: str, url: str | None) -> tuple[str, float] | None:
    """What today's rules make of a stored funding signal, or None when it stays funding."""
    from nexus.ingestion.sources import _classify_news, is_profile_page

    if source == "web_news" and is_profile_page(url or ""):
        return "news", 0.4
    kind, strength = _classify_news(title or "")
    return None if kind == "funding" else (kind, strength)


async def sweep(*, apply: bool = False, session=None) -> dict:
    """Re-classify stored funding signals. Returns counts; writes only when ``apply``."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.signal import SignalEvent

    async def run(s) -> dict:
        # Cross-tenant maintenance: the platform sessionmaker (owner role), or RLS returns zero rows
        # and this reports a clean table it never looked at.
        rows = (await s.execute(
            select(SignalEvent).where(SignalEvent.kind == "funding",
                                      SignalEvent.source.in_(SOURCES))
        )).scalars().all()
        changed: Counter[str] = Counter()
        samples: list[str] = []
        for signal in rows:
            verdict = reclassified(signal.source, signal.title, signal.url)
            if verdict is None:
                continue
            changed[signal.source] += 1
            if len(samples) < 12:
                samples.append(f"{signal.source:8} -> {verdict[0]:8} | {signal.title[:80]}")
            if apply:
                signal.kind, signal.strength = verdict
        if apply:
            await s.commit()
        return {"scanned": len(rows), "changed": sum(changed.values()),
                "by_source": dict(changed), "samples": samples}

    if session is not None:
        return await run(session)
    async with get_platform_sessionmaker()() as own:
        return await run(own)


async def main(apply: bool) -> None:
    result = await sweep(apply=apply)
    verb = "Re-classified" if apply else "Would re-classify"
    print(f"Scanned {result['scanned']} stored funding signals.")
    print(f"  {verb} {result['changed']}: {result['by_source']}")
    for line in result["samples"]:
        print(f"    {line}")
    if not apply:
        print("\nDry run: nothing was written. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    asyncio.run(main(parser.parse_args().apply))
