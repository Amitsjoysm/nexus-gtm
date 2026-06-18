"""Backfill: re-verify every tenant's contact emails against the live verifier.

One-off / repeatable maintenance pass for data written while the email verifier was unreachable
(those contacts kept a guessed address with no deliverability status -> "unverified" in the UI).
Run as the DB *owner* (the worker role), which can read the ``tenants`` table and every tenant's
contacts:

    docker compose -f docker-compose.prod.yml exec worker python scripts/reverify_contacts.py

Idempotent and safe to re-run. Requires ``NEXUS_EMAIL_VERIFY_PROVIDER=reacher`` (or another real
provider) and a reachable ``NEXUS_EMAIL_VERIFY_URL``; with the stub verifier it's a no-op.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from nexus.core.db import get_sessionmaker
from nexus.enrichment.reverify import reverify_contacts
from nexus.workers.tasks import tenant_session


async def main() -> None:
    async with get_sessionmaker()() as session:
        tenant_ids = [r[0] for r in (await session.execute(text("SELECT id FROM tenants"))).all()]

    grand_checked = grand_updated = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as ts:
            res = await reverify_contacts(ts, only_unverified=True)
        grand_checked += res["checked"]
        grand_updated += res["updated"]
        if res["checked"]:
            print(f"tenant {tid}: checked={res['checked']} updated={res['updated']} {res['statuses']}")

    print(f"\nDONE — {grand_updated} updated across {grand_checked} checked "
          f"in {len(tenant_ids)} tenant(s).")


if __name__ == "__main__":
    asyncio.run(main())
