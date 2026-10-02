#!/usr/bin/env python
"""Derive LinkedIn-shaped codes for shared companies stored before they were derived on write.

`companies.resolution.derive_codes` gives every shared row written from now on an industry code,
an HQ country and a size band read off its own industry, country and headcount, so another
workspace's ICP search can offer it (behind that workspace's fit threshold). Rows already in the
table were written before that and stay invisible to every search until something writes them
again. Measured on the local deploy, 2026-10-02: 256 of 393 shared companies.

Dry run by default: it reports what it would set. ``--apply`` writes. Fills blanks only, never
touches a row LinkedIn has read, and is safe to run repeatedly (a second run finds nothing to do).

**Run it where the database is**, as `repair_feed_text.py` explains; the target is printed first.

    docker cp scripts/derive_company_codes.py nexus-gtm-app-1:/tmp/derive.py
    docker exec nexus-gtm-app-1 python /tmp/derive.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/derive.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the codes (default: report only)")
    args = ap.parse_args()

    from sqlalchemy import or_, select

    from nexus.companies.resolution import (
        derive_codes,
        derived_country_code,
        derived_industry_id,
    )
    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    settings = get_settings()
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database:", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))
    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")

    tally: Counter = Counter()
    async with get_platform_sessionmaker()() as session:
        rows = (await session.scalars(
            select(Company)
            .where(Company.linkedin_fetched_at.is_(None))
            .where(or_(
                Company.linkedin_industry_id.is_(None) & Company.industry.isnot(None),
                Company.hq_country_code.is_(None) & Company.country.isnot(None),
                Company.employee_range_min.is_(None) & Company.employee_count.isnot(None),
            )))).all()
        tally["examined"] = len(rows)
        for row in rows:
            if row.linkedin_industry_id is None and derived_industry_id(row.industry):
                tally["industry"] += 1
            if row.hq_country_code is None and derived_country_code(row.country):
                tally["country"] += 1
            if row.employee_range_min is None and row.employee_count:
                tally["size"] += 1
            if args.apply:
                await derive_codes(session, row)
        if args.apply:
            await session.commit()
    print(f"examined {tally['examined']} rows without LinkedIn data: "
          f"industry code for {tally['industry']}, country for {tally['country']}, "
          f"size band for {tally['size']}" + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
