"""Measure the automation heartbeat's real throughput. k6 cannot see any of this.

The heartbeat is off-request, so no HTTP load test touches it. This runs the ACTUAL production
code paths — ``handle_refresh_due_accounts``, ``process_account``, ``run_worker`` — against a
database of a given size and reports wall-clock numbers.

Point ``NEXUS_DATABASE_URL`` at a SCRATCH database (see the README), never the live one: [A]
stamps ``last_refreshed_at`` on every account it claims, which in production would suppress those
accounts' next real refresh. ``NEXUS_QUEUE_BACKEND`` is forced to ``memory`` here so the enqueued
jobs go nowhere near live Valkey.

    NEXUS_DATABASE_URL=postgresql+asyncpg://...@postgres:5432/nexus_lt python bench_heartbeat.py

[B] and [C] remove the signal sources deliberately: that isolates the DB + scoring + inbox + plays
cost, which is the *floor*. The real per-account cost is that floor plus the network crawl, and
the crawl is better measured from ``signal_source_runs`` on a live stack than from synthetic
accounts whose domains resolve to nothing.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time

os.environ["NEXUS_QUEUE_BACKEND"] = "memory"  # never touch live Valkey
os.environ.setdefault("NEXUS_AUTOMATION_ENABLED", "true")

from nexus.core.config import get_settings  # noqa: E402
from nexus.core.db import get_sessionmaker  # noqa: E402
from nexus.workers.queue import InMemoryTaskQueue, Job, set_task_queue  # noqa: E402


def report(label: str, xs: list[float]) -> None:
    xs = sorted(xs)
    n = len(xs)
    pick = lambda q: xs[min(n - 1, int(q * n))]  # noqa: E731
    print(
        f"  {label:<38} n={n:<4} mean={statistics.mean(xs) * 1000:8.1f}ms  "
        f"p50={pick(.5) * 1000:8.1f}ms  p95={pick(.95) * 1000:8.1f}ms  max={xs[-1] * 1000:8.1f}ms"
    )


async def bench_claim_driver(reps: int) -> None:
    """[A] the whole handle_refresh_due_accounts driver: claim query + stamp + enqueue."""
    from nexus.workers.tasks import handle_refresh_due_accounts

    if reps <= 0:
        return
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    print(f"\n[A] handle_refresh_due_accounts (batch={get_settings().account_refresh_batch_size})")
    times: list[float] = []
    counts: list[int] = []
    for _ in range(reps):
        started = time.perf_counter()
        result = await handle_refresh_due_accounts({})
        times.append(time.perf_counter() - started)
        counts.append(result.get("accounts", 0))
    report("driver wall time", times)
    print(f"  accounts claimed per call: {counts}")
    print(f"  -> enqueue capacity while running: "
          f"{statistics.mean(counts) / statistics.mean(times):.2f} accounts/sec")


async def _sample_accounts(n: int) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from nexus.models.account import Account

    async with get_sessionmaker()() as session:
        return list((await session.execute(select(Account.tenant_id, Account.id).limit(n))).all())


async def bench_process_account(n: int) -> None:
    """[B] process_account with the network sources removed — the pure DB/scoring floor."""
    from nexus.ingestion.service import get_ingestion_service
    from nexus.models.account import Account
    from nexus.pipeline import process_account
    from nexus.workers.tasks import tenant_session

    rows = await _sample_accounts(n) if n > 0 else []
    if not rows:
        return
    service = get_ingestion_service()
    original, service.sources = service.sources, []
    print("\n[B] process_account, sources removed (DB + scoring + inbox + plays floor)")
    times: list[float] = []
    try:
        for tenant_id, account_id in rows:
            started = time.perf_counter()
            async with tenant_session(tenant_id) as ts:
                await process_account(ts, await ts.get(Account, account_id))
            times.append(time.perf_counter() - started)
    finally:
        service.sources = original
    report("process_account (no network)", times)
    print(f"  -> serial floor: {1 / statistics.mean(times):.2f} accounts/sec/worker")


def _limits() -> list[int | None]:
    """Concurrency caps to probe, as ``run_worker(concurrency=...)`` values.

    ``1`` reproduces the pre-M-perf serial loop exactly, which is what makes the before/after in
    the README a like-for-like comparison rather than two numbers from two builds. ``None`` means
    "whatever the DB pool allows" — the shipped default.
    """
    raw = os.environ.get("BENCH_CONC_LIMITS", "1,default")
    out: list[int | None] = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        out.append(None if token == "default" else int(token))
    return out


async def _drain_with_worker(n: int, done: asyncio.Event, limit: int | None, budget: float) -> float:
    """Run ``run_worker`` until ``done``, and return the wall time it took.

    Waits on completion of the Nth job rather than on the queue emptying: with jobs in flight the
    queue empties long before the work is finished, and timing that instead would flatter the
    concurrent numbers by exactly the amount being measured.
    """
    from nexus.workers.worker import run_worker

    stop = asyncio.Event()
    started = time.perf_counter()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.05, concurrency=limit))
    try:
        await asyncio.wait_for(done.wait(), timeout=budget)
        return time.perf_counter() - started
    finally:
        stop.set()
        await task


def _report_drain(label: str, n: int, elapsed: float, limit: int | None, unit: str) -> None:
    from nexus.workers.worker import resolve_worker_concurrency

    shown = resolve_worker_concurrency() if limit is None else limit
    tag = f"cap={shown}{' (pool-derived default)' if limit is None else ''}"
    print(f"  {label:<28} {tag:<30} {elapsed:7.2f}s -> {n / elapsed:6.2f} {unit}")


async def bench_worker_drain(n: int) -> None:
    """[C] the real run_worker loop draining N jobs. Measures the loop, not the handler."""
    from nexus.ingestion.service import get_ingestion_service
    from nexus.workers import tasks as tasks_module
    from nexus.workers.worker import run_worker  # noqa: F401  (imported for the probe's contract)

    rows = await _sample_accounts(n) if n > 0 else []
    if not rows:
        return
    service = get_ingestion_service()
    original, service.sources = service.sources, []
    real_handler = tasks_module.HANDLERS["process_account"]
    print(f"\n[C] run_worker draining {len(rows)} process_account jobs (sources removed)")
    try:
        for limit in _limits():
            completed = 0
            done = asyncio.Event()

            async def counted(payload: dict) -> dict:
                nonlocal completed
                result = await real_handler(payload)
                completed += 1
                if completed >= len(rows):
                    done.set()
                return result

            tasks_module.HANDLERS["process_account"] = counted
            queue = InMemoryTaskQueue()
            set_task_queue(queue)
            for tenant_id, account_id in rows:
                await queue.enqueue(
                    Job(
                        name="process_account",
                        payload={"tenant_id": tenant_id, "account_id": account_id},
                    )
                )
            elapsed = await _drain_with_worker(len(rows), done, limit, budget=600.0)
            _report_drain(f"{len(rows)} accounts", len(rows), elapsed, limit, "accounts/sec")
    finally:
        tasks_module.HANDLERS["process_account"] = real_handler
        service.sources = original


async def bench_worker_concurrency(n: int, sleep_s: float) -> None:
    """[D] is run_worker serial or concurrent? N jobs x sleep_s. Serial => ~N*sleep_s.

    A sleep stands in for the real handler on purpose: ``process_account`` is ~99%
    await-on-network, so this measures the property that actually governs throughput — how many
    of those waits the loop is willing to overlap — without a third-party API in the loop.
    """
    from nexus.workers import tasks as tasks_module

    if n <= 0:
        return

    print(f"\n[D] run_worker concurrency probe: {n} jobs x {sleep_s}s sleep "
          f"(serial ~{n * sleep_s:.1f}s)")
    try:
        for limit in _limits():
            completed = 0
            done = asyncio.Event()

            async def slow(payload: dict) -> dict:
                nonlocal completed
                await asyncio.sleep(sleep_s)
                completed += 1
                if completed >= n:
                    done.set()
                return {"ok": True}

            tasks_module.HANDLERS["bench_sleep"] = slow
            queue = InMemoryTaskQueue()
            set_task_queue(queue)
            for i in range(n):
                await queue.enqueue(Job(name="bench_sleep", payload={"i": i}))
            elapsed = await _drain_with_worker(n, done, limit, budget=n * sleep_s + 120.0)
            _report_drain(f"{n} jobs", n, elapsed, limit, "jobs/sec")
            print(f"  {'':<28} {'':<30} effective concurrency = "
                  f"{n * sleep_s / elapsed:.2f}")
    finally:
        tasks_module.HANDLERS.pop("bench_sleep", None)


async def main() -> None:
    from sqlalchemy import func, select

    from nexus.models.account import Account

    settings = get_settings()
    print(f"db            : {settings.database_url.split('@')[-1]}")
    print(f"tick interval : {settings.automation_tick_interval_s}s")
    print(f"batch size    : {settings.account_refresh_batch_size}")
    print(f"refresh window: {settings.account_refresh_interval_s}s")
    async with get_sessionmaker()() as session:
        total = await session.scalar(select(func.count()).select_from(Account))
    print(f"accounts      : {total:,}")
    print(f"cold interval : {settings.account_refresh_interval_cold_s}s")
    print(f"config enqueue ceiling: "
          f"{settings.account_refresh_batch_size / settings.automation_tick_interval_s:.2f} "
          f"accounts/sec")

    # Demand is no longer one number: it depends on how much of the estate is HOT, which is a
    # property of the customer's data rather than of the config. Printed as a curve so the target
    # can be read against a real hot ratio instead of assuming the worst case, and so the untiered
    # figure stays visible as the thing being improved on.
    hot_s = settings.account_refresh_interval_s
    cold_s = settings.account_refresh_interval_cold_s
    print(f"\nsteady-state demand (accounts/sec), by share of the estate that is HOT:")
    print(f"  {'hot %':>6}  {'demand':>8}")
    for pct in (100, 50, 25, 15, 10, 5, 0):
        hot = total * pct / 100
        print(f"  {pct:>5}%  {hot / hot_s + (total - hot) / cold_s:>8.2f}"
              f"{'   <- untiered' if pct == 100 else ''}")

    await bench_claim_driver(int(os.environ.get("BENCH_CLAIM_REPS", 5)))
    await bench_process_account(int(os.environ.get("BENCH_PROC_N", 30)))
    await bench_worker_drain(int(os.environ.get("BENCH_DRAIN_N", 50)))
    await bench_worker_concurrency(
        int(os.environ.get("BENCH_CONC_N", 20)), float(os.environ.get("BENCH_CONC_SLEEP", 1.0))
    )


if __name__ == "__main__":
    asyncio.run(main())
