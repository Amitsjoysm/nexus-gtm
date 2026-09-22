"""Who answered the signal searches, what was reused, and what is still paid for.

Reports, never repairs — like `nexus/billing/reconcile.py` and `nexus/companies/diff.py`. Read it
asymmetrically: a shadow comparison where only the PAID engine found something (`paid_only`) is what
promoting the self-hosted fetcher would lose. Cache hits are the saving already banked.

Usage:
    python scripts/fetch_shadow_report.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import timedelta

#: Firecrawl search, Hobby tier — the `signal.news_scan` cost in `nexus/billing/rates.py`.
PAID_COST_PER_QUERY = 0.0064
#: Answers that cost nothing to obtain.
_FREE = {"cache", "nexusfetch", "", "unknown"}


def summarise(provenances: list[dict]) -> dict:
    """Aggregate dork provenance rows (`signal_source_runs.provenance`) into one report."""
    answered: Counter[str] = Counter()
    cached = failed = 0
    shadow = {"compared": 0, "paid_only": 0, "both": 0, "free_only": 0}
    for provenance in provenances:
        for query in (provenance or {}).get("queries", []):
            if query.get("cached"):
                cached += 1
                continue
            if query.get("failed"):
                failed += 1
                continue
            answered[query.get("answered_by") or "unknown"] += 1
            report = query.get("shadow")
            if report:
                free = report.get("nexusfetch") or 0
                paid = max((v or 0) for k, v in report.items() if k != "nexusfetch") \
                    if len(report) > 1 else 0
                shadow["compared"] += 1
                if paid and not free:
                    shadow["paid_only"] += 1
                elif free and not paid:
                    shadow["free_only"] += 1
                elif free and paid:
                    shadow["both"] += 1
    return {
        "total": cached + failed + sum(answered.values()),
        "cached": cached,
        "failed": failed,
        "answered": dict(answered),
        "shadow": shadow,
        "paid_queries": sum(n for name, n in answered.items() if name not in _FREE),
    }


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


def render(summary: dict, *, days: int) -> str:
    total = summary["total"]
    lines = [f"Dork queries in the last {days} days: {total}",
             f"  served from cache  {summary['cached']:>7}  {_pct(summary['cached'], total):>6}"
             "   bought nothing"]
    for name, count in sorted(summary["answered"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  answered by {name:<9}{count:>7}  {_pct(count, total):>6}")
    lines.append(f"  failed             {summary['failed']:>7}  {_pct(summary['failed'], total):>6}")
    paid = summary["paid_queries"]
    lines.append(f"\nStill paid for: {paid} queries, about ${paid * PAID_COST_PER_QUERY:.2f}")
    s = summary["shadow"]
    if s["compared"]:
        lines += [
            f"\nShadow comparisons: {s['compared']}",
            f"  both found results      {s['both']:>6}",
            f"  only the fetcher found  {s['free_only']:>6}",
            f"  ONLY PAID FOUND         {s['paid_only']:>6}  {_pct(s['paid_only'], s['compared'])}"
            "   <- what promotion would lose",
        ]
    return "\n".join(lines)


async def main(days: int) -> None:
    # Imported here so `summarise` can be tested without a database.
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.source_run import SignalSourceRun

    # `signal_source_runs` is tenant-scoped, so this cross-tenant read MUST use the platform
    # sessionmaker: under the app's RLS-bound role it returns zero rows and prints a platform
    # nobody uses — the trap this codebase has walked into three times.
    since = utcnow() - timedelta(days=days)
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(SignalSourceRun.provenance).where(
                SignalSourceRun.started_at >= since, SignalSourceRun.source == "dork",
            )
        )).scalars().all()
    print(render(summarise(list(rows)), days=days))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    asyncio.run(main(parser.parse_args().days))
