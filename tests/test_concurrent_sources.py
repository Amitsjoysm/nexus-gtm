# tests/test_concurrent_sources.py
"""Signal sources run concurrently, without losing anything the sequential path recorded.

Sources are ~99% await-on-network, so running them one after another spends the sum of their
latencies for nothing. Measured over 355 real crawls on the live stack: sum = 26.98s, slowest
single source = 14.94s — a 1.81x cut in per-account wall time at no CPU cost.

The risk is not speed, it is bookkeeping. `signal_source_runs` is the evidence that tells a broken
source apart from a quiet market, and it has to stay identical however the sources were scheduled.
The other risk is the session: a change detector borrows the caller's TenantSession, and
SQLAlchemy's AsyncSession is not safe for concurrent use.
"""
from __future__ import annotations

import asyncio
import time

from tests.conftest import make_tenant


class _SlowSource:
    """A source that sleeps and records the interval it was running.

    Concurrency is asserted from OVERLAPPING INTERVALS, never from total wall time. A duration
    assertion looks like a fine test until the suite runs `-n auto` on a loaded machine, where a
    starved event loop makes concurrent work take as long as sequential and the test fails for a
    reason that has nothing to do with the code.
    """

    def __init__(self, name: str, delay: float, items: list | None = None):
        self.name = name
        self.delay = delay
        self._items = items or []
        self.last_provenance = {"q": name}
        self.started_at: float | None = None
        self.ended_at: float | None = None

    async def fetch(self, account):
        self.started_at = time.perf_counter()
        try:
            await asyncio.sleep(self.delay)
            return list(self._items)
        finally:
            self.ended_at = time.perf_counter()

    def overlaps(self, other: "_SlowSource") -> bool:
        if None in (self.started_at, self.ended_at, other.started_at, other.ended_at):
            return False
        return self.started_at < other.ended_at and other.started_at < self.ended_at


class _SessionBoundSource(_SlowSource):
    """Mirrors the change detector: needs the caller's session, so it must NOT be gathered."""

    def __init__(self, name: str, delay: float, seen: list):
        super().__init__(name, delay)
        self._seen = seen
        self.bound = None

    def bind_session(self, ts):
        self.bound = ts

    async def fetch(self, account):
        # Record concurrency: if two session-bound sources ever overlap, this list interleaves.
        self._seen.append(f"{self.name}:start")
        await asyncio.sleep(self.delay)
        self._seen.append(f"{self.name}:end")
        return []


async def _account(ts):
    from nexus.models.account import Account

    acct = Account(name="Acme", domain="acme.com")
    ts.add(acct)
    await ts.flush()
    return acct


async def test_sources_run_concurrently(fresh_db, monkeypatch):
    """Four sources at 0.3s each: sequential is ~1.2s, concurrent is ~0.3s."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.workers.tasks import tenant_session

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_sources_concurrent", True)
    monkeypatch.setattr(settings, "source_timeout_s", 5)

    svc = get_ingestion_service()
    sources = [_SlowSource(f"s{i}", 0.2) for i in range(4)]
    monkeypatch.setattr(svc, "sources", sources)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)

    # Every source overlapped every other one. True regardless of how loaded the machine is.
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            assert a.overlaps(b), f"{a.name} and {b.name} did not overlap — ran sequentially"


async def test_the_kill_switch_restores_sequential_behaviour(fresh_db, monkeypatch):
    """If a provider turns out to rate-limit on concurrency, this must fix it without a deploy."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.workers.tasks import tenant_session

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_sources_concurrent", False)
    monkeypatch.setattr(settings, "source_timeout_s", 5)

    svc = get_ingestion_service()
    sources = [_SlowSource(f"s{i}", 0.05) for i in range(4)]
    monkeypatch.setattr(svc, "sources", sources)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)

    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            assert not a.overlaps(b), f"{a.name} and {b.name} overlapped — kill switch ignored"


async def test_every_source_is_still_recorded(fresh_db, monkeypatch):
    """`signal_source_runs` is what tells a broken source apart from a quiet market. Concurrency
    must not lose a row, an outcome, or a duration."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.models.source_run import SignalSourceRun
    from nexus.workers.tasks import tenant_session

    monkeypatch.setattr(get_settings(), "signal_sources_concurrent", True)
    monkeypatch.setattr(get_settings(), "source_timeout_s", 5)

    svc = get_ingestion_service()
    monkeypatch.setattr(svc, "sources", [_SlowSource(f"s{i}", 0.05) for i in range(4)])

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)
        runs = await ts.list(SignalSourceRun)

    assert {r.source for r in runs} == {"s0", "s1", "s2", "s3"}
    # Found nothing, so `empty` — deliberately not `ok`, per the M16 rule.
    assert all(r.outcome == "empty" for r in runs)
    assert all(r.duration_ms > 0 for r in runs)
    assert all((r.provenance or {}).get("q") == r.source for r in runs)


async def test_one_failing_source_does_not_take_down_the_others(fresh_db, monkeypatch):
    """A broken source must not stop ingestion — the property gather() would break if the
    exception escaped."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.models.source_run import SignalSourceRun
    from nexus.workers.tasks import tenant_session

    monkeypatch.setattr(get_settings(), "signal_sources_concurrent", True)
    monkeypatch.setattr(get_settings(), "source_timeout_s", 5)

    class _Broken(_SlowSource):
        async def fetch(self, account):
            raise RuntimeError("upstream is down")

    svc = get_ingestion_service()
    monkeypatch.setattr(
        svc, "sources", [_SlowSource("good", 0.05), _Broken("bad", 0), _SlowSource("also", 0.05)]
    )

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)          # must not raise
        runs = {r.source: r for r in await ts.list(SignalSourceRun)}

    assert runs["bad"].outcome == "error"
    assert "upstream is down" in runs["bad"].error
    assert runs["good"].outcome == "empty"
    assert runs["also"].outcome == "empty"


async def test_a_slow_source_still_times_out_on_its_own_budget(fresh_db, monkeypatch):
    """Per-source timeouts must survive gather, or one hanging provider stalls every account."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.models.source_run import SignalSourceRun
    from nexus.workers.tasks import tenant_session

    monkeypatch.setattr(get_settings(), "signal_sources_concurrent", True)
    monkeypatch.setattr(get_settings(), "source_timeout_s", 0.2)

    svc = get_ingestion_service()
    monkeypatch.setattr(svc, "sources", [_SlowSource("quick", 0.02), _SlowSource("hang", 3)])

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)
        runs = {r.source: r for r in await ts.list(SignalSourceRun)}

    # The hang was 3s against a 0.2s budget; recording it as `timeout` at all is the proof the
    # per-source deadline survived the gather.
    assert runs["hang"].outcome == "timeout"
    assert runs["quick"].outcome == "empty"
    assert runs["hang"].duration_ms < 1500, "the per-source timeout did not apply inside gather"


async def test_session_bound_sources_are_never_gathered(fresh_db, monkeypatch):
    """THE correctness constraint. A change detector borrows the caller's TenantSession, and
    SQLAlchemy's AsyncSession is not safe for concurrent use — two coroutines awaiting on one
    session interleave on a single connection and raise, or return each other's rows.

    Asserted structurally: the two session-bound sources must not overlap in time."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.workers.tasks import tenant_session

    monkeypatch.setattr(get_settings(), "signal_sources_concurrent", True)
    monkeypatch.setattr(get_settings(), "source_timeout_s", 5)

    seen: list[str] = []
    bound_a = _SessionBoundSource("a", 0.1, seen)
    bound_b = _SessionBoundSource("b", 0.1, seen)
    svc = get_ingestion_service()
    # TWO network sources, deliberately: with only one there is nothing to gather, the code takes
    # the all-sequential path, and this test would pass without ever exercising the split it
    # exists to check.
    monkeypatch.setattr(
        svc, "sources", [_SlowSource("net1", 0.1), _SlowSource("net2", 0.1), bound_a, bound_b],
    )

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        await svc.run_sources(ts, acct)

    # Strictly serialised: each start is immediately followed by its own end.
    assert seen == ["a:start", "a:end", "b:start", "b:end"], seen
    # And they still received the caller's session, which is why they are excluded.
    assert bound_a.bound is not None and bound_b.bound is not None


async def test_signals_from_every_source_are_ingested(fresh_db, monkeypatch):
    """Concurrency must not drop results — the collected list is filled from several coroutines."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service
    from nexus.ingestion.sources import RawSignal
    from nexus.workers.tasks import tenant_session

    monkeypatch.setattr(get_settings(), "signal_sources_concurrent", True)
    monkeypatch.setattr(get_settings(), "source_timeout_s", 5)

    def _sig(n: str) -> RawSignal:
        return RawSignal(kind="news", source=n, title=f"{n} headline", strength=0.6,
                         dedupe_key=f"dk-{n}")

    svc = get_ingestion_service()
    monkeypatch.setattr(
        svc, "sources",
        [_SlowSource("s1", 0.05, [_sig("s1")]), _SlowSource("s2", 0.05, [_sig("s2")])],
    )

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = await _account(ts)
        created = await svc.run_sources(ts, acct)

    assert {c.title for c in created} == {"s1 headline", "s2 headline"}
