"""The daily crawl budget stops what spends money, never the free sources (2026-10-15).

`tenant_daily_source_runs` (400) refused the WHOLE crawl once reached. Measured on the local
deploy, that left about 82-92 of ~220 accounts per workspace crawled a day, though only the dork
source buys searches: RSS, job boards, the public APIs and keyless web news cost nothing. Decided
with the product owner: past the budget only paid sources are skipped. The count still includes
every run, so paid spend and paid coverage are exactly what they were.
"""
from __future__ import annotations

from nexus.ingestion.service import IngestionService
from nexus.models.account import Account
from tests.conftest import make_tenant, tenant_session


class _Source:
    def __init__(self, name: str, *, costs_money: bool):
        self.name = name
        self.costs_money = costs_money
        self.fetched = 0

    async def fetch(self, account):
        self.fetched += 1
        return []


async def _crawl(monkeypatch, *, over: bool):
    paid, free = _Source("dork", costs_money=True), _Source("rss", costs_money=False)
    service = IngestionService([paid, free])

    async def budget(self, ts, cap):
        return over

    monkeypatch.setattr(IngestionService, "_over_daily_budget", budget)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(account)
        await ts.flush()
        await service.run_sources(ts, account)
    return paid, free


async def test_past_the_budget_only_the_paid_source_is_skipped(monkeypatch):
    paid, free = await _crawl(monkeypatch, over=True)
    assert paid.fetched == 0, "a paid search ran past the budget"
    assert free.fetched == 1, "a free source was refused by a spend guard"


async def test_under_the_budget_everything_runs(monkeypatch):
    paid, free = await _crawl(monkeypatch, over=False)
    assert paid.fetched == 1 and free.fetched == 1


def test_only_the_dork_source_says_it_costs_money():
    from nexus.ingestion import sources as s

    assert s.SignalSource.costs_money is True, "a new source must be budgeted until it says not"
    assert s.DorkedSearchSource.costs_money is True
    for cls in (s.RssSignalSource, s.AtsSignalSource, s.PublicApiSignalSource, s.WebNewsSource,
                s.WebsiteWatchSignalSource, s.DemoSignalSource):
        assert cls.costs_money is False, cls.__name__
