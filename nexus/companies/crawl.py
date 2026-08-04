# nexus/companies/crawl.py
"""Shared company crawl — step 3, running in SHADOW.

Crawls a company once and writes `company_signals`. **Nothing reads those rows yet.** That is the
entire point of this step: correctness is proved on live data while a wrong result costs a re-run
rather than a customer-visible error, because no tenant's inbox is downstream of it.

The failure history that justifies the shadow period: six wrong-attribution bugs in the signal
subsystem, four of which were found only by running against real providers. A shared store
multiplies that blast radius by the number of tenants, so it does not get switched on by assertion.

It reuses the existing per-tenant sources unchanged. Building a second crawler would mean two
implementations that drift, and the diff harness would then be comparing two bugs.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.companies.crawl")


def _as_account(company):
    """Adapt a Company to the shape the signal sources expect.

    The sources take an ``Account`` and read four attributes off it. Rather than change every source
    signature — and risk the per-tenant path that currently works — hand them a lightweight stand-in
    carrying the same fields. It is never persisted and never leaves this module.
    """
    from nexus.models.account import Account

    return Account(
        tenant_id="",                    # shared crawl: there is no tenant, and nothing writes
        id=company.id,
        name=company.name or company.domain,
        domain=company.domain,
        industry=company.industry,
        employee_count=company.employee_count,
        tech_stack=list(company.tech_stack or []),
    )


async def crawl_company(company, *, sources=None) -> dict:
    """Fetch signals for one company and upsert them into `company_signals`.

    Never raises: this runs on the worker beside the per-tenant sweep, and a shadow feature must not
    be able to disturb the pipeline that customers actually depend on.
    """
    import asyncio

    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.company import CompanySignal

    report = {"company_id": company.id, "domain": company.domain, "found": 0, "new": 0}
    try:
        if sources is None:
            from nexus.ingestion.service import get_ingestion_service

            # The same sources the per-tenant path uses. WebsiteWatchSignalSource is excluded: it
            # needs a tenant-scoped baseline (`page_snapshots`), so it has no meaning here.
            sources = [
                s for s in get_ingestion_service().sources
                if not hasattr(s, "bind_session")
            ]

        stand_in = _as_account(company)
        timeout = get_settings().source_timeout_s
        collected = []
        for src in sources:
            try:
                budget = getattr(src, "timeout_s", None) or timeout
                collected.extend(await asyncio.wait_for(src.fetch(stand_in), timeout=budget))
            except Exception:
                # Per-source isolation, same as the per-tenant service: one dead provider must not
                # cost the signals the others found.
                logger.warning("shared crawl source %s failed for %s",
                               getattr(src, "name", src), company.domain, exc_info=True)
        report["found"] = len(collected)

        async with get_platform_sessionmaker()() as session:
            existing = {
                key for (key,) in (
                    await session.execute(
                        select(CompanySignal.dedupe_key).where(
                            CompanySignal.company_id == company.id
                        )
                    )
                ).all()
            }
            for raw in collected:
                key = (raw.dedupe_key or "")[:200]
                if not key or key in existing:
                    continue
                existing.add(key)
                session.add(CompanySignal(
                    company_id=company.id,
                    kind=raw.kind,
                    source=raw.source,
                    title=(raw.title or "")[:400],
                    body=raw.body,
                    url=(raw.url or "")[:500] if raw.url else None,
                    strength=raw.resolved_strength(),
                    dedupe_key=key,
                    occurred_at=raw.occurred_at,
                ))
                report["new"] += 1
            # Stamped whether or not anything was found: the stamp means "we looked", and without
            # that the due-scan would pick the same company forever.
            db_company = await session.get(type(company), company.id)
            if db_company is not None:
                db_company.last_crawled_at = utcnow()
            await session.commit()
    except Exception:
        logger.warning("shared crawl failed for %s", company.domain, exc_info=True)
        report["error"] = "crawl failed; see logs"
    return report


async def crawl_due_companies(*, limit: int = 20, max_age_hours: int = 6) -> dict:
    """Crawl companies whose shared record is stale. Bounded per call; never raises.

    Deliberately a smaller batch than the per-tenant sweep: this is additive load on the same
    providers while the per-tenant path is still doing all the real work, and the shadow period
    should not double anyone's rate-limit pressure.
    """
    from datetime import timedelta

    from sqlalchemy import or_, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.company import Company

    report = {"scanned": 0, "crawled": 0, "signals": 0}
    try:
        cutoff = utcnow() - timedelta(hours=max_age_hours)
        async with get_platform_sessionmaker()() as session:
            companies = (
                await session.scalars(
                    select(Company)
                    .where(or_(Company.last_crawled_at.is_(None),
                               Company.last_crawled_at <= cutoff))
                    .order_by(Company.last_crawled_at.asc().nulls_first())
                    .limit(limit)
                )
            ).all()
            # Detach: crawl_company opens its own session, and holding this one across the network
            # calls would pin a connection for the whole batch.
            targets = [(c.id, c.domain, c.name, c.industry, c.employee_count,
                        list(c.tech_stack or [])) for c in companies]
        report["scanned"] = len(targets)

        from nexus.models.company import Company as C

        for cid, domain, name, industry, employees, stack in targets:
            stub = C(id=cid, domain=domain, name=name, industry=industry,
                     employee_count=employees, tech_stack=stack)
            result = await crawl_company(stub)
            report["crawled"] += 1
            report["signals"] += result.get("new", 0)
    except Exception:
        logger.warning("shared crawl sweep failed", exc_info=True)
        report["error"] = "sweep failed; see logs"
    return report
