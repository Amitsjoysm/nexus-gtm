# nexus/companies/fanout.py
"""Project shared company signals into each subscribing tenant — step 5.

The step that makes the shared crawl actually save anything, and the one that can degrade a working
pipeline, so it ships **behind a flag defaulting off** and only after the diff harness reports
agreement.

**It reuses `IngestionService.ingest` rather than writing `signal_events` directly.** That is the
whole design. Ingest already owns per-tenant dedupe, the `(tenant_id, dedupe_key)` unique
constraint, `signal.created`, and — since the alerting fix — creating alerts in the same
transaction. A second write path would have to reimplement all of it and would drift; the first
thing to drift would be alerts, and the symptom would be signals appearing with nobody notified,
which is exactly the bug that shipped once already.

So fan-out's only job is: find the tenants, convert the rows, and hand them to the code that
already works.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.companies.fanout")


def _to_raw(signal):
    """A shared `CompanySignal` as the `RawSignal` the ingestion path expects.

    The dedupe key is carried across unchanged. It was computed by `event_dedupe_key` during the
    shared crawl, so a tenant that already has this event from its own per-tenant crawl matches and
    is skipped — the two paths agree by construction rather than by coincidence.
    """
    from nexus.ingestion.sources import RawSignal

    return RawSignal(
        kind=signal.kind,
        source=signal.source or "shared",
        title=signal.title,
        dedupe_key=signal.dedupe_key,
        body=signal.body,
        url=signal.url,
        strength=signal.strength,
        occurred_at=signal.occurred_at,
    )


async def fanout_company(company_id: str, *, limit_tenants: int = 500) -> dict:
    """Deliver one company's shared signals to every tenant that tracks it.

    Never raises. Fan-out is an optimisation layered on a working per-tenant pipeline; a failure
    here must degrade to "that tenant keeps its own crawl", never to lost signals.
    """
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.ingestion.service import IngestionService
    from nexus.models.account import Account
    from nexus.models.company import CompanySignal

    report = {"company_id": company_id, "tenants": 0, "delivered": 0}
    if not get_settings().shared_company_crawl_enabled:
        report["skipped"] = "disabled"
        return report

    try:
        async with get_platform_sessionmaker()() as session:
            # Global flag on, but each company still earns delivery individually. A company whose
            # shared crawl has never been compared with the per-tenant one keeps its per-tenant
            # crawl — `pipeline._covered_by_shared_crawl` tests the same condition, so an unproven
            # company is crawled exactly as it was before this subsystem existed.
            from nexus.models.company import Company

            company = await session.get(Company, company_id)
            if company is None or company.crawl_verdict != "agrees":
                report["skipped"] = "unproven"
                return report
            shared = (
                await session.scalars(
                    select(CompanySignal).where(CompanySignal.company_id == company_id)
                )
            ).all()
            if not shared:
                return report
            # Archived accounts are excluded: a deleted account must not resurrect itself with a
            # fresh batch of signals.
            targets = (
                await session.scalars(
                    select(Account)
                    .where(
                        Account.company_id == company_id,
                        Account.archived_at.is_(None),
                    )
                    .limit(limit_tenants)
                )
            ).all()
            pairs = [(a.tenant_id, a.id) for a in targets]
            raws = [_to_raw(s) for s in shared]

        report["tenants"] = len({tid for tid, _ in pairs})
        service = IngestionService(sources=[])
        for tenant_id, account_id in pairs:
            try:
                async with get_platform_sessionmaker()() as session:
                    # A hand-built TenantSession must bind RLS first or writes are rejected against
                    # Postgres — the trap documented in CLAUDE.md.
                    await apply_rls(session, tenant_id)
                    ts = TenantSession(session, tenant_id)
                    account = await ts.first(Account, Account.id == account_id)
                    if account is None:
                        continue
                    created = await service.ingest(ts, account, raws)
                    await session.commit()
                    report["delivered"] += len(created)
            except Exception:
                # Per-tenant isolation: one workspace's failure must not stop delivery to the rest.
                logger.warning("fan-out to tenant %s failed for company %s",
                               tenant_id, company_id, exc_info=True)
    except Exception:
        logger.warning("fan-out failed for company %s", company_id, exc_info=True)
        report["error"] = "fan-out failed; see logs"
    return report


async def fanout_due_companies(*, limit: int = 20) -> dict:
    """Fan out recently-crawled companies. Bounded per call; never raises."""
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    report = {"companies": 0, "delivered": 0}
    if not get_settings().shared_company_crawl_enabled:
        report["skipped"] = "disabled"
        return report
    try:
        async with get_platform_sessionmaker()() as session:
            ids = (
                await session.scalars(
                    select(Company.id)
                    .where(Company.last_crawled_at.is_not(None))
                    .order_by(Company.last_crawled_at.desc())
                    .limit(limit)
                )
            ).all()
        for company_id in ids:
            result = await fanout_company(company_id)
            report["companies"] += 1
            report["delivered"] += result.get("delivered", 0)
    except Exception:
        logger.warning("fan-out sweep failed", exc_info=True)
        report["error"] = "sweep failed; see logs"
    return report


async def backfill_account_from_shared(ts, account) -> int:
    """Deliver a company's existing shared signals to ONE account, in the caller's transaction.

    Without this, a rep adding an account whose company is already proven sees an empty timeline:
    the per-tenant crawl steps aside (the shared crawl covers it) and the next fan-out sweep has not
    run yet. That is precisely the "zero signals after 30 minutes" complaint the first-crawl-on-
    create path exists to prevent — reintroduced by the cost optimisation.

    Goes through ``IngestionService.ingest``, so per-tenant dedupe, ``signal.created`` and
    same-transaction alerting all apply exactly as for a live crawl. Returns how many landed.
    """
    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.service import IngestionService
    from nexus.models.company import CompanySignal

    company_id = getattr(account, "company_id", None)
    if not company_id:
        return 0
    try:
        from sqlalchemy import select as _select

        async with get_platform_sessionmaker()() as session:
            shared = (
                await session.scalars(
                    _select(CompanySignal).where(CompanySignal.company_id == company_id)
                )
            ).all()
            raws = [_to_raw(sig) for sig in shared]
        if not raws:
            return 0
        created = await IngestionService(sources=[]).ingest(ts, account, raws)
        return len(created)
    except Exception:
        # Never break account creation for a backfill. Worst case the rep waits for the next sweep,
        # which is the behaviour before this function existed.
        logger.warning("shared backfill failed for account %s", getattr(account, "id", "?"),
                       exc_info=True)
        return 0
