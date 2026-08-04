# nexus/companies/diff.py
"""Compare shared-crawl output against per-tenant output — step 4.

The gate between the shadow crawl and fan-out. Nothing switches on until this reports agreement,
because a shared store multiplies any attribution mistake by the number of tenants subscribed to
that company.

**It reports; it never repairs.** Which side is right depends on when each crawl ran and what the
providers returned at the time — an automated writer would resolve that wrongly and destroy the
evidence needed to understand the disagreement. The same reasoning as `billing/reconcile.py`.

Read the output as: `only_shared` is usually fine (the shared crawl ran more recently, or the
per-tenant one was budget-skipped). `only_tenant` is the one that matters — it means the shared
crawl is MISSING something a tenant already has, and fan-out would look like data loss.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.companies.diff")


@dataclass(slots=True)
class CompanyDiff:
    """Agreement between the two crawls for one account."""

    account_id: str = ""
    company_id: str = ""
    domain: str = ""
    shared_only: list[str] = field(default_factory=list)
    tenant_only: list[str] = field(default_factory=list)
    both: int = 0

    @property
    def agrees(self) -> bool:
        """Agreement means the shared crawl is not MISSING anything the tenant has.

        Extra shared signals are not a disagreement: the shared crawl may simply have run more
        recently. A missing one is, because fan-out would then present less than the tenant has
        today, which reads as data loss.
        """
        return not self.tenant_only

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "company_id": self.company_id,
            "domain": self.domain,
            "both": self.both,
            "shared_only": self.shared_only[:20],
            "tenant_only": self.tenant_only[:20],
            "agrees": self.agrees,
        }


async def diff_account(session, account) -> CompanyDiff | None:
    """Compare one account's signals with its company's. None when it is not linked yet."""
    from sqlalchemy import select

    from nexus.models.company import CompanySignal
    from nexus.models.signal import SignalEvent

    if not account.company_id:
        return None

    shared = {
        key for (key,) in (
            await session.execute(
                select(CompanySignal.dedupe_key).where(
                    CompanySignal.company_id == account.company_id
                )
            )
        ).all() if key
    }
    tenant = {
        key for (key,) in (
            await session.execute(
                select(SignalEvent.dedupe_key).where(
                    SignalEvent.account_id == account.id
                )
            )
        ).all() if key
    }
    return CompanyDiff(
        account_id=account.id,
        company_id=account.company_id,
        domain=account.domain or "",
        shared_only=sorted(shared - tenant),
        tenant_only=sorted(tenant - shared),
        both=len(shared & tenant),
    )


async def diff_sample(*, limit: int = 50) -> dict:
    """Diff a sample of linked accounts. Returns a report; never raises.

    Cross-tenant by nature, so it runs through the platform sessionmaker — under the RLS-bound role
    it would compare zero rows against zero rows and report perfect agreement, which is the most
    dangerous possible false negative for a gate like this.
    """
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account

    report = {"compared": 0, "agree": 0, "disagree": 0, "details": []}
    try:
        async with get_platform_sessionmaker()() as session:
            accounts = (
                await session.scalars(
                    select(Account).where(Account.company_id.is_not(None)).limit(limit)
                )
            ).all()
            for account in accounts:
                result = await diff_account(session, account)
                if result is None:
                    continue
                report["compared"] += 1
                if result.agrees:
                    report["agree"] += 1
                else:
                    report["disagree"] += 1
                    # Only disagreements are detailed. A report listing every agreement is one
                    # nobody reads, and the whole value here is that a disagreement stands out.
                    report["details"].append(result.as_dict())
    except Exception:
        logger.warning("company diff sample failed", exc_info=True)
        report["error"] = "diff failed; see logs"
    return report


async def record_verdict(session, company_id: str, agrees: bool) -> str:
    """Persist what a comparison concluded about one company.

    Read asymmetrically, exactly as ``CompanyDiff.agrees`` is: ``shared_only`` is usually fine (the
    shared crawl ran more recently), while ``tenant_only`` is the failure — fan-out would deliver
    less than the tenant already has.

    A disagreement is recorded, not just withheld. "We compared and it was wrong" and "we never
    compared" call for different actions, and collapsing them into one absent verdict hides the
    former behind the latter.
    """
    from datetime import datetime, timezone

    from nexus.models.company import Company

    company = await session.get(Company, company_id)
    if company is None:
        return "unknown"
    company.crawl_verdict = "agrees" if agrees else "disagrees"
    company.verdict_at = datetime.now(timezone.utc)
    await session.flush()
    return company.crawl_verdict
