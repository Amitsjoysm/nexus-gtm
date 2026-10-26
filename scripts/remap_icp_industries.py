#!/usr/bin/env python
"""Map every saved ICP's industries to LinkedIn codes again, with the current mapping.

Saving an ICP stores the LinkedIn industry codes its industries map to, and the fit score and the
company search read those stored codes. When the mapping improves (`industries.MAPPING_VERSION`),
codes stored by an older one stay wrong until somebody changes the words: measured 2026-10-26,
"Shipping" was filed under *Boilers, Tanks, and Shipping Container Manufacturing*, "Distribution"
under *Natural Gas Distribution* and "Energy" under *Services for Renewable Energy*.

Dry run by default: it reports each ICP whose codes would change. ``--apply`` writes. Only the
derived codes and their version are written; the industries a person typed are never touched. Safe
to run repeatedly (a second run finds nothing to do). Terms the deterministic mapping cannot place
are asked of the LLM from a shortlist, exactly as saving the ICP does.

**Run it where the database is**, as `repair_feed_text.py` explains; the target is printed first.

    docker cp scripts/remap_icp_industries.py nexus-gtm-app-1:/tmp/remap.py
    docker exec nexus-gtm-app-1 python /tmp/remap.py            # report only
    docker exec nexus-gtm-app-1 python /tmp/remap.py --apply    # write
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
    ap.add_argument("--apply", action="store_true", help="write the codes (default: report only)")
    args = ap.parse_args()

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.relevance import RelevanceProfile
    from nexus.prospecting.industries import MAPPING_VERSION, map_industries

    settings = get_settings()
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database:", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))
    print("mode:", "APPLY" if args.apply else "dry run (pass --apply to write)")

    examined = changed = 0
    # Every workspace's profile, so the owner role: under the RLS-bound role this reads nothing.
    async with get_platform_sessionmaker()() as session:
        profiles = (await session.scalars(select(RelevanceProfile))).all()
        for profile in profiles:
            icp = dict(profile.icp or {})
            industries = list(icp.get("industries") or [])
            if not industries or icp.get("linkedin_industry_map_version") == MAPPING_VERSION:
                continue
            examined += 1
            before = list(icp.get("linkedin_industry_ids") or [])
            after = await map_industries(industries)
            if after != before:
                changed += 1
                print(f"  tenant {profile.tenant_id}: {industries} {before} -> {after}")
            if args.apply:
                icp["linkedin_industry_ids"] = after
                icp["linkedin_industry_map_version"] = MAPPING_VERSION
                profile.icp = icp           # a new dict, so the JSON change is tracked
        if args.apply:
            await session.commit()
    print(f"{changed} of {examined} saved ICPs from an older mapping have different codes now"
          + ("" if args.apply else " (nothing written)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
