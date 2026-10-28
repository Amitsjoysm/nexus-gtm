#!/usr/bin/env python
"""Hold back shared companies stored under a website that is not theirs (2026-10-28).

An acquired or subsidiary company's LinkedIn page lists its owner's website, and before
`prospecting/vetting.py` existed every such page was stored under that domain: on a copy of the
local database, chevron.com was "Hess Corporation" (4,337 staff) and kfrc.co (a bit.ly link) was
Kforce, offered to every workspace whose ICP matched. New reads are vetted; this handles the rows
stored before.

Decided with the product owner 2026-10-28: a row the guard refuses is QUARANTINED, never deleted,
so it is offered to nobody. The wrong page is also detached (``linkedin_url`` and what that read
wrote, recorded in ``field_sources["linkedin_url"]``), because `store_companies` attaches a page
only to a row that has none: left in place, Hess's page would keep Chevron's own page out for
good. A row the wrong read CREATED loses everything that read wrote; a row that existed before keeps
its own values. Accounts are not changed (fix forward only); how many link to each row is reported
so a person can review them.

Dry run by default: it lists what it would hold back and asks only the homepages of rows whose
domain does not name them (about 6%). ``--apply`` writes. Safe to run again.

**Run it where the database is**, as `repair_feed_text.py` explains; the target is printed first.

    docker cp scripts/vet_linkedin_companies.py nexus-gtm-app-1:/tmp/vet.py
    docker exec nexus-gtm-app-1 python /tmp/vet.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/vet.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="quarantine the rows (default: report)")
    args = ap.parse_args()

    from sqlalchemy import delete, func, select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.account import Account
    from nexus.models.company import Company, CompanyCountry
    from nexus.prospecting.vetting import vet

    settings = get_settings()
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database:", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))
    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")

    # The shared store and every workspace's accounts, so the owner role.
    async with get_platform_sessionmaker()() as session:
        rows = (await session.scalars(
            select(Company)
            .where(Company.linkedin_url.isnot(None))
            .where(Company.labels_quarantined_at.is_(None))
            .order_by(Company.domain))).all()
        _, refused = await vet(rows)
        for row in refused:
            linked = await session.scalar(
                select(func.count()).select_from(Account).where(Account.company_id == row.id))
            print(f"  {row.domain:30} | {row.name[:40]:40} | {row.linkedin_url} | "
                  f"{linked} account(s) linked")
            if not args.apply:
                continue
            sources = dict(row.field_sources or {})
            sources["linkedin_url"] = {
                "source": "vetting", "ref": row.linkedin_url, "at": utcnow().isoformat(),
                "detail": f"page of {row.name!r} detached: its website is another company's"}
            if row.source == "linkedin":
                # Everything on a row the wrong read created came from that page.
                row.name, row.industry, row.country, row.employee_count = "", None, None, None
                row.description = None
                await session.execute(delete(CompanyCountry).where(
                    CompanyCountry.company_id == row.id))
            row.linkedin_url = row.linkedin_id = row.linkedin_industry_id = None
            row.employee_range_min = row.employee_range_max = row.hq_country_code = None
            row.similar_linkedin = None
            row.linkedin_fetched_at = None
            row.field_sources = sources
            row.labels_quarantined_at = utcnow()
        if args.apply:
            await session.commit()
    verb = "quarantined" if args.apply else "would quarantine"
    print(f"{verb} {len(refused)} of {len(rows)} LinkedIn-read shared rows"
          + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
