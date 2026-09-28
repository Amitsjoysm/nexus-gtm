# scripts/migrate_engagement.py
"""Move every workspace from the old Campaigns and Cadences to the engagement engine (spec §13).

Run the dry run first and read it with the owner. It writes nothing, and it reports:
sequences to move, sequences that will pause until their owner connects a mailbox, the old emails
found and not found in Sent folders, and campaigns whose drafts move to review.

    python scripts/migrate_engagement.py --dry-run
    python scripts/migrate_engagement.py --dry-run --tenant <tenant_id>
    python scripts/migrate_engagement.py                 # for real, every workspace
    python scripts/migrate_engagement.py --json          # machine-readable report

Idempotent: run it again at any time, including after SDRs connect mailboxes, which imports the
history that had nowhere to live the first time. Each workspace runs in its own RLS-bound session
and its own transaction, so one workspace failing leaves the others migrated and says which.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select, union


async def _tenants(only: str | None) -> list[str]:
    """Workspaces with anything to move: a cadence or a campaign."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.cadence import Cadence
    from nexus.models.campaign import Campaign

    if only:
        return [only]
    async with get_platform_sessionmaker()() as session:
        rows = await session.execute(union(select(Cadence.tenant_id), select(Campaign.tenant_id)))
        return sorted({tenant_id for (tenant_id,) in rows})


async def main(*, dry_run: bool, tenant: str | None, as_json: bool) -> int:
    from nexus.engagement.cutover.migrate import migrate, render
    from nexus.workers.tasks import tenant_session

    failures = 0
    reports = []
    tenants = await _tenants(tenant)
    if not tenants and not as_json:
        # Silence reads as "did it run?" to the operator following the runbook; say it did.
        print("No workspace has old Campaigns or Cadences to move. Nothing to do.")
    for tenant_id in tenants:
        try:
            async with tenant_session(tenant_id) as ts:
                report = await migrate(ts, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - report the workspace, carry on with the rest
            failures += 1
            print(f"Workspace {tenant_id}: FAILED, nothing written ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            continue
        reports.append(report)
        if not as_json:
            print(render(report))
            print()
    if as_json:
        print(json.dumps([{**r.as_dict(), "totals": r.totals} for r in reports], indent=2,
                         default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("--tenant", help="one workspace id")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, tenant=args.tenant, as_json=args.json)))
