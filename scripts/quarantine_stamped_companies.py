#!/usr/bin/env python
"""Hold back shared companies whose industry and country were an ICP's words stamped onto a web find.

Until 2026-10-26 the web step of ICP discovery wrote its workspace's FIRST ICP industry and FIRST
country onto every hit (`tests/test_web_hit_labels.py`). The backfill copied those values into the
shared ``companies`` row, ``derive_codes`` coded them, and the chain's shared step then offered the
row to every other workspace whose ICP named the same industry. Measured on the local deploy: 202
shared rows; all 65 "Manufacturing" accounts one workspace received were another's web finds.

Decided with the product owner: existing ACCOUNTS keep their values (fix forward only); the
stamped shared rows are quarantined (``labels_quarantined_at``) and not offered again until a
LinkedIn read labels them, which lifts the quarantine (`prospecting/companies.store_companies`).

A row is stamped when the account that CREATED it (the earliest linked) was a web find from before
the fix: ``source`` ``auto_discovery`` or ``discovery``, never read by LinkedIn. Later accounts got
their label FROM the row, so they prove nothing; an uploaded, CRM or hand-typed label is the
customer's own and is left alone.

Dry run by default: it reports what it would quarantine. ``--apply`` writes. Safe to run again.

**Run it where the database is**, as `repair_feed_text.py` explains; the target is printed first.

    docker cp scripts/quarantine_stamped_companies.py nexus-gtm-app-1:/tmp/quarantine.py
    docker exec nexus-gtm-app-1 python /tmp/quarantine.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/quarantine.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The web step stopped stamping on this day; a web find from it on carries its own enrichment.
STAMP_FIXED = datetime(2026, 10, 26, tzinfo=timezone.utc)
#: How the web step's accounts are recorded (`deliver(..., source=)`): the daily sweep and Populate,
#: and the Orchestrator.
WEB_FIND_SOURCES = ("auto_discovery", "discovery")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="quarantine the rows (default: report)")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import ensure_aware, get_platform_sessionmaker, utcnow
    from nexus.models.account import Account
    from nexus.models.company import Company

    settings = get_settings()
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database:", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))
    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")

    examined = stamped = 0
    # Every workspace's accounts, so the owner role: under the RLS-bound role this reads nothing.
    async with get_platform_sessionmaker()() as session:
        rows = (await session.scalars(
            select(Company)
            .where(Company.linkedin_fetched_at.is_(None))
            .where(Company.labels_quarantined_at.is_(None))
            .where(Company.industry.isnot(None) | Company.country.isnot(None)))).all()
        for row in rows:
            examined += 1
            creator = (await session.scalars(
                select(Account).where(Account.company_id == row.id)
                .order_by(Account.created_at.asc(), Account.id).limit(1))).first()
            if creator is None or creator.source not in WEB_FIND_SOURCES:
                continue
            if ensure_aware(creator.created_at) >= STAMP_FIXED:
                continue
            stamped += 1
            if args.apply:
                row.labels_quarantined_at = utcnow()
        if args.apply:
            await session.commit()
    verb = "quarantined" if args.apply else "would quarantine"
    print(f"{verb} {stamped} of {examined} shared rows LinkedIn has not read"
          + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
