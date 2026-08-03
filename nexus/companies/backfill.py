# nexus/companies/backfill.py
"""Point existing accounts at shared company records.

Step 2 of the migration path in docs/superpowers/plans/2026-07-31-master-company-data-layer.md.

Three properties make this safe to run on a live database, repeatedly, without a window:

* **It only ever fills a NULL.** An account already linked is skipped, so a partial run followed by
  a full one is identical to one full run.
* **It is bounded per call**, so it never holds a long transaction on the accounts table.
* **It writes nothing anyone reads yet.** Nothing consumes `company_id` until fan-out is enabled
  behind its flag, so a wrong link at this stage costs a re-run, not a customer-visible error.

Accounts with no usable domain are counted as `skipped_no_domain` and left alone — deliberately, and
permanently. Resolving them by name would merge unrelated businesses across tenants.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.companies.backfill")


async def backfill_companies(*, limit: int = 1000, dry_run: bool = False) -> dict:
    """Link up to `limit` unlinked accounts. Returns a report; never raises."""
    from sqlalchemy import select

    from nexus.companies.resolution import normalise_domain, resolve_company
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account

    report = {"scanned": 0, "linked": 0, "created": 0, "skipped_no_domain": 0, "dry_run": dry_run}
    try:
        # Cross-tenant maintenance, so the owner role: under the RLS-bound role this scan matches
        # zero rows and would report a clean backfill on a database it never touched.
        async with get_platform_sessionmaker()() as session:
            accounts = (
                await session.scalars(
                    select(Account).where(Account.company_id.is_(None)).limit(limit)
                )
            ).all()
            report["scanned"] = len(accounts)

            seen_before: set[str] = set()
            for account in accounts:
                domain = normalise_domain(account.domain)
                if not domain:
                    report["skipped_no_domain"] += 1
                    continue
                if dry_run:
                    report["linked"] += 1
                    continue
                company = await resolve_company(
                    session,
                    domain=account.domain,
                    name=account.name or "",
                    industry=account.industry,
                    country=account.country,
                    employee_count=account.employee_count,
                    tech_stack=list(account.tech_stack or []),
                    source="account_backfill",
                )
                if company is None:
                    report["skipped_no_domain"] += 1
                    continue
                # Flush per company so a later account in this batch resolving the same domain
                # finds the row rather than racing to insert a second one.
                await session.flush()
                if company.id not in seen_before:
                    seen_before.add(company.id)
                    report["created"] += 1
                account.company_id = company.id
                report["linked"] += 1
            if not dry_run:
                await session.commit()
    except Exception:
        logger.warning("company backfill failed", exc_info=True)
        report["error"] = "backfill failed; see logs"
    return report
