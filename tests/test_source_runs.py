"""Crawl history and the demotion of synthetic signals (M16).

Two problems, one milestone:

* The default pipeline emitted **synthetic** signals, so every score, alert and play built on an
  out-of-the-box deployment described events that never happened.
* A source that ran and found nothing and a source that had been broken for a week produced
  identical evidence — none. The first sign of trouble was a rep asking why an obviously-funded
  account showed no round.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_tenant, tenant_session


async def _account(tid: str, name: str = "Acme Corp"):
    from nexus.models.account import Account

    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name=name, domain="acme.com")
        ts.add(acct)
        await ts.flush()
        return acct.id


class Yielding:
    name = "yielding"

    def __init__(self, items):
        self._items = items

    async def fetch(self, account):
        return list(self._items)


class Broken:
    name = "broken"

    async def fetch(self, account):
        raise RuntimeError("provider exploded")


class Slow:
    name = "slow"
    timeout_s = 0.05

    async def fetch(self, account):
        await asyncio.sleep(5)
        return []


async def _runs(tid: str):
    from nexus.models.source_run import SignalSourceRun

    async with tenant_session(tid) as ts:
        return await ts.list(SignalSourceRun)


# ---- synthetic signals are no longer the default ----------------------------------------------

def test_the_default_pipeline_is_real_sources():
    """It was "demo". An out-of-the-box deployment scored and alerted on fabricated events."""
    from nexus.core.config import Settings

    assert Settings().signal_sources == "web,rss"
    assert "demo" not in Settings().signal_sources


def test_production_refuses_to_start_with_synthetic_signals():
    """`demo_signals_active` already forced them off in prod, but silently — an operator who asked
    for demo signals and got none could not tell that from a broken pipeline."""
    from nexus.core.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(env="prod", secret_key="x" * 40, signal_sources="demo,rss")
    assert "demo" in str(exc.value)

    with pytest.raises(ValueError):
        Settings(env="staging", secret_key="x" * 40, signal_sources="demo")


def test_local_and_test_may_still_use_the_double():
    """The offline suite injects it explicitly; making it unusable everywhere would break every
    fixture that depends on deterministic signals."""
    from nexus.core.config import Settings

    assert Settings(env="local", signal_sources="demo").signal_sources == "demo"
    assert Settings(env="test", signal_sources="demo").demo_signals_active is True


def test_production_still_hard_disables_the_double_flag():
    """Belt and braces: the validator catches the source list, this catches the flag."""
    from nexus.core.config import Settings

    s = Settings(env="prod", secret_key="x" * 40, demo_signals_enabled=True)
    assert s.demo_signals_active is False


# ---- crawl history ----------------------------------------------------------------------------

async def test_a_run_is_recorded_even_when_nothing_is_found():
    """The entire point: absence of signals becomes evidence rather than silence."""
    from nexus.ingestion.service import IngestionService
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr1")
    aid = await _account(tid)
    svc = IngestionService(sources=[Yielding([])])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await svc.run_sources(ts, acct)

    rows = await _runs(tid)
    assert len(rows) == 1
    # `empty`, not `ok`: a source that runs cleanly and finds nothing every time is broken, and
    # merging the two states hides exactly that.
    assert rows[0].outcome == "empty"
    assert rows[0].source == "yielding"
    assert rows[0].items_found == 0
    assert rows[0].finished_at is not None


async def test_a_successful_run_counts_found_and_new_separately():
    """A large gap between them means the source keeps re-finding the same event — the cost
    signature worth watching."""
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr2")
    aid = await _account(tid)
    items = [
        RawSignal(kind="funding", source="yielding", title="Acme raises", dedupe_key="f:1"),
        RawSignal(kind="news", source="yielding", title="Acme partners", dedupe_key="n:1"),
    ]
    svc = IngestionService(sources=[Yielding(items)])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await svc.run_sources(ts, acct)
        # Second pass: the same two signals are found again but none of them are new.
        await svc.run_sources(ts, acct)

    rows = sorted(await _runs(tid), key=lambda r: r.started_at)
    assert [r.items_found for r in rows] == [2, 2]
    assert [r.items_new for r in rows] == [2, 0]
    assert all(r.outcome == "ok" for r in rows)


async def test_a_failing_source_is_recorded_with_its_error():
    from nexus.ingestion.service import IngestionService
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr3")
    aid = await _account(tid)
    svc = IngestionService(sources=[Broken()])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await svc.run_sources(ts, acct)

    rows = await _runs(tid)
    assert rows[0].outcome == "error"
    assert "provider exploded" in rows[0].error


async def test_a_timeout_is_its_own_outcome():
    """Distinct from `error`: a slow source needs a bigger budget, a broken one needs a fix."""
    from nexus.ingestion.service import IngestionService
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr4")
    aid = await _account(tid)
    svc = IngestionService(sources=[Slow()])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await svc.run_sources(ts, acct)

    rows = await _runs(tid)
    assert rows[0].outcome == "timeout"
    assert "timed out" in rows[0].error


async def test_one_broken_source_does_not_stop_the_others():
    """Per-source isolation, now visible in the history rather than only in the logs."""
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr5")
    aid = await _account(tid)
    good = Yielding([RawSignal(kind="funding", source="yielding", title="x", dedupe_key="f:9")])
    svc = IngestionService(sources=[Broken(), good])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        created = await svc.run_sources(ts, acct)

    assert len(created) == 1
    outcomes = {r.source: r.outcome for r in await _runs(tid)}
    assert outcomes == {"broken": "error", "yielding": "ok"}


async def test_a_failed_history_write_does_not_lose_the_signals(monkeypatch):
    """Bookkeeping must never cost the thing it describes."""
    from nexus.ingestion import service as svc_mod
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import RawSignal
    from nexus.models.account import Account

    tid = await make_tenant(slug="sr6")
    aid = await _account(tid)
    svc = IngestionService(
        sources=[Yielding([RawSignal(kind="funding", source="yielding", title="x",
                                     dedupe_key="f:7")])]
    )

    async def boom(*_a, **_kw):
        raise RuntimeError("history table gone")

    monkeypatch.setattr(svc_mod.IngestionService, "_record_runs", boom)
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        with pytest.raises(RuntimeError):
            await svc.run_sources(ts, acct)
    # The signal itself was still committed before the history write was attempted.
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        assert len(await ts.list(SignalEvent)) == 1


async def test_provenance_records_the_queries_that_were_run():
    """Without the rendered queries, "why did this find nothing?" is unanswerable after the fact:
    the query depends on the account, the date and the provider's dialect."""
    from nexus.ingestion.service import IngestionService
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.models.account import Account

    class Empty:
        name = "fake"
        query_dialect = "keyword"

        async def search_recent(self, query, *, limit=5, days=90, include_domains=(),
                                exclude_domains=()):
            return []

    tid = await make_tenant(slug="sr7")
    aid = await _account(tid)
    svc = IngestionService(sources=[DorkedSearchSource(search=Empty(), max_queries=2)])
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await svc.run_sources(ts, acct)

    rows = await _runs(tid)
    prov = rows[0].provenance
    assert prov["provider"] == "fake"
    assert prov["dialect"] == "keyword"
    assert len(prov["queries"]) == 2
    assert all(q["query"] for q in prov["queries"])


async def test_runs_are_tenant_scoped():
    """The table carries tenant_id, so apply_rls.py enrols it — a crawl belongs to one workspace."""
    from nexus.models.source_run import SignalSourceRun

    assert "tenant_id" in SignalSourceRun.__table__.columns


def test_source_run_metrics_are_recorded():
    from nexus.core import metrics

    before = metrics.metric_value(
        "nexus_signal_source_runs_total", {"source": "probe", "outcome": "empty"}
    ) or 0
    metrics.record_source_run("probe", "empty", 0)
    after = metrics.metric_value(
        "nexus_signal_source_runs_total", {"source": "probe", "outcome": "empty"}
    ) or 0
    assert after == before + 1
