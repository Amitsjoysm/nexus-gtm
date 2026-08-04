# tests/test_company_crawl_saving.py
"""The shared company crawl has to REPLACE per-tenant work, not add to it.

Until this seam existed the company layer was pure additional cost: every account was still crawled
per tenant, and the shared crawl ran on top. Forty workspaces tracking Stripe crawled it forty-one
times instead of once. The saving only appears when the per-tenant crawl steps aside for accounts
the shared crawl already covers.

The conditions are deliberately narrow, because the failure mode of getting this wrong is an
account that silently stops receiving signals — indistinguishable from a quiet market:

* the flag is on,
* the account is linked to a shared company, AND
* that company has actually been crawled at least once.

Any of those missing and the per-tenant crawl runs exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _account(ts, *, name="Acme", domain="acme.com", company_id=None):
    from nexus.models.account import Account

    account = Account(name=name, domain=domain, company_id=company_id)
    ts.add(account)
    await ts.flush()
    return account


async def _company(domain="acme.com", *, crawled: bool):
    """A shared company row, optionally already crawled."""
    from nexus.companies.resolution import company_id_for, normalise_domain
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    normalised = normalise_domain(domain)
    async with get_platform_sessionmaker()() as session:
        company = Company(
            id=company_id_for(normalised), domain=normalised, name=normalised,
            last_crawled_at=datetime.now(timezone.utc) if crawled else None,
        )
        session.add(company)
        await session.commit()
        return company.id


class _CountingIngestion:
    """Stands in for the real ingestion service and records whether it was asked to crawl."""

    def __init__(self):
        self.calls = 0

    async def run_sources(self, ts, account):
        self.calls += 1
        return []


async def _run(monkeypatch, *, flag: bool, company_id, crawled=True):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import set_ingestion_service
    from nexus.pipeline import process_account

    counter = _CountingIngestion()
    set_ingestion_service(counter)
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", flag)

    cid = await _company(crawled=crawled) if company_id else None
    tid = await make_tenant(slug=f"cs{abs(hash((flag, company_id, crawled))) % 9999}", name="CS")
    async with tenant_session(tid) as ts:
        account = await _account(ts, company_id=cid)
        result = await process_account(ts, account)
    return counter.calls, result


async def test_the_per_tenant_crawl_is_skipped_for_a_covered_account(monkeypatch):
    """The whole point: one crawl for the company, not one per tenant."""
    calls, result = await _run(monkeypatch, flag=True, company_id=True)
    assert calls == 0
    assert result.get("signals_source") == "shared_company"


async def test_scoring_still_runs_for_a_skipped_account(monkeypatch):
    """Only the crawl is shared. Scoring, inbox and plays are per-tenant and must be untouched —
    skipping them would turn a cost optimisation into a broken workspace."""
    _, result = await _run(monkeypatch, flag=True, company_id=True)
    assert "composite_score" in result
    assert result.get("scoring_status") != "skipped"


async def test_an_unlinked_account_is_still_crawled_per_tenant(monkeypatch):
    """An account with no usable domain never joins the shared store, so it keeps its own crawl.
    That is the documented safety property, not a gap."""
    calls, _ = await _run(monkeypatch, flag=True, company_id=None)
    assert calls == 1


async def test_a_linked_but_never_crawled_company_does_not_stop_the_tenant_crawl(monkeypatch):
    """Backfill links accounts long before the shared crawler reaches them. Skipping on the link
    alone would black out every newly-linked account until the shared crawl caught up."""
    calls, _ = await _run(monkeypatch, flag=True, company_id=True, crawled=False)
    assert calls == 1


async def test_nothing_changes_while_the_flag_is_off(monkeypatch):
    """Default off. The shadow period has to leave the working pipeline exactly as it was."""
    calls, _ = await _run(monkeypatch, flag=False, company_id=True)
    assert calls == 1
