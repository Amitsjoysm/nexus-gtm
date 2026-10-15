"""Platform health's Search row reads what the searches did, not what is configured (2026-10-15).

It returned "ok" with the provider names whatever happened. Measured on the local deploy: the
dork source found nothing for eight days while Firecrawl answered 402 and 429, and the row said ok
the whole time. It now reads the last day of crawl history for the search-backed sources, across
every workspace (the platform session: under the app role a cross-tenant read returns zero rows).
"""
from __future__ import annotations

from datetime import timedelta

from nexus.core.db import utcnow
from tests.conftest import make_tenant, tenant_session


async def _runs(tid: str, source: str, outcome: str, n: int, *, found: int = 0,
                error: str = "", hours_ago: float = 1.0) -> None:
    from nexus.models.source_run import SignalSourceRun

    started = utcnow() - timedelta(hours=hours_ago)
    async with tenant_session(tid) as ts:
        for _ in range(n):
            ts.add(SignalSourceRun(source=source, outcome=outcome, items_found=found,
                                   error=error, started_at=started))


async def test_failing_searches_are_an_error_naming_the_reason(fresh_db):
    from nexus.ingestion.health import search_health

    tid = await make_tenant()
    await _runs(tid, "dork", "error", 8,
                error="provider unavailable: 2-key pool rate-limited after retries")
    await _runs(tid, "dork", "empty", 2)
    status, detail = await search_health()
    assert status == "error", detail
    assert "dork" in detail and "8 of 10 failed" in detail
    assert "rate-limited" in detail


async def test_a_source_that_finds_nothing_all_day_is_degraded(fresh_db):
    from nexus.ingestion.health import search_health

    tid = await make_tenant()
    await _runs(tid, "dork", "empty", 25)
    status, detail = await search_health()
    assert status == "degraded", detail
    assert "found nothing in 25 runs" in detail


async def test_working_searches_are_ok(fresh_db):
    from nexus.ingestion.health import search_health

    tid = await make_tenant()
    await _runs(tid, "dork", "ok", 5, found=2)
    await _runs(tid, "dork", "empty", 30)
    await _runs(tid, "web_news", "ok", 3, found=1)
    status, detail = await search_health()
    assert status == "ok", detail
    assert "dork 35 runs, 10 found" in detail


async def test_it_reads_every_workspace(fresh_db):
    from nexus.ingestion.health import search_health

    for slug in ("s1", "s2"):
        await _runs(await make_tenant(slug=slug), "dork", "error", 3,
                    error="provider unavailable: x")
    status, detail = await search_health()
    assert status == "error" and "6 of 6 failed" in detail


async def test_old_runs_and_free_sources_do_not_count(fresh_db):
    from nexus.ingestion.health import search_health

    tid = await make_tenant()
    await _runs(tid, "dork", "error", 9, error="old outage", hours_ago=30)
    await _runs(tid, "rss", "empty", 40)
    status, detail = await search_health()
    assert status == "ok", detail
    assert "no search-backed runs in the last 24h" in detail


async def test_platform_health_reports_it(fresh_db, monkeypatch):
    from nexus.api.routers import admin_health

    tid = await make_tenant()
    await _runs(tid, "dork", "error", 4, error="provider unavailable: dead")
    status, detail = await admin_health._probe_search()
    assert status == "error"
    assert detail.startswith("signals=") and "4 of 4 failed" in detail
