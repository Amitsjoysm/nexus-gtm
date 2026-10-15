"""Whether signal searches are working, read from what they did (2026-10-15).

Platform health's Search row used to name the configured providers and say "ok", so eight days of
Firecrawl answering 402 and 429, with the dork source finding nothing, read as healthy. This reads
the crawl history the searches leave behind (``signal_source_runs``) for the last day, across every
workspace, which is the evidence an operator would otherwise have to query by hand.

Only the sources that buy searches are read. RSS, job boards and the public APIs are keyless and
fail for their own reasons; folding them in would let a working feed hide a dead search provider.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: The sources whose results come from a paid or rate-limited search backend.
SEARCH_SOURCES = ("dork", "web_news")
#: A source that ran this often in a day and found nothing is broken, not quiet: `empty` is not
#: `ok` (see `SignalSourceRun.outcome`).
NOTHING_FOUND_RUNS = 20
WINDOW = timedelta(hours=24)


async def search_health(*, now: datetime | None = None) -> tuple[str, str]:
    """``(status, detail)``: ``error`` when half the runs of a search source failed, ``degraded``
    when any failed or one ran all day and found nothing, ``ok`` otherwise. Never raises."""
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.source_run import SignalSourceRun

    since = (now or utcnow()) - WINDOW
    try:
        async with get_platform_sessionmaker()() as session:
            rows = (await session.execute(
                select(SignalSourceRun.source, SignalSourceRun.outcome, func.count(),
                       func.coalesce(func.sum(SignalSourceRun.items_found), 0))
                .where(SignalSourceRun.source.in_(SEARCH_SOURCES),
                       SignalSourceRun.started_at >= since)
                .group_by(SignalSourceRun.source, SignalSourceRun.outcome)
            )).all()
            latest_error = (await session.execute(
                select(SignalSourceRun.source, SignalSourceRun.error)
                .where(SignalSourceRun.source.in_(SEARCH_SOURCES),
                       SignalSourceRun.started_at >= since,
                       SignalSourceRun.outcome == "error")
                .order_by(SignalSourceRun.started_at.desc())
                .limit(1)
            )).first()
    except Exception as exc:  # health must report, never take the console down
        return "degraded", f"could not read the crawl history: {type(exc).__name__}"

    tally: dict[str, dict[str, int]] = {}
    for source, outcome, count, found in rows:
        t = tally.setdefault(source, {"runs": 0, "failed": 0, "found": 0})
        t["runs"] += int(count)
        t["found"] += int(found or 0)
        if outcome in ("error", "timeout"):
            t["failed"] += int(count)
    if not tally:
        return "ok", "no search-backed runs in the last 24h"

    status, parts = "ok", []
    for source in SEARCH_SOURCES:
        t = tally.get(source)
        if not t:
            continue
        line = f"{source} {t['runs']} runs, {t['found']} found"
        if t["failed"]:
            line += f", {t['failed']} of {t['runs']} failed"
            status = "error" if t["failed"] * 2 >= t["runs"] else _worst(status, "degraded")
        elif t["runs"] >= NOTHING_FOUND_RUNS and t["found"] == 0:
            line += f": found nothing in {t['runs']} runs"
            status = _worst(status, "degraded")
        parts.append(line)
    detail = "last 24h: " + "; ".join(parts)
    if latest_error is not None:
        detail += f". Latest failure ({latest_error[0]}): {(latest_error[1] or '')[:200]}"
    return status, detail


def _worst(a: str, b: str) -> str:
    order = {"ok": 0, "degraded": 1, "error": 2}
    return a if order[a] >= order[b] else b
