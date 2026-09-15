"""Postgres connection budget for NEXUS on Azure Container Apps.

The capacity model in docs/quality/load-capacity.md is generated from this, so the arithmetic is
re-checkable instead of believed:

    python scripts/load/capacity.py                # staging and production, markdown
    python scripts/load/capacity.py --sku B_Standard_B2s --pool 5 --overflow 5

Two numbers per row. ``ceiling`` is what the pools are ALLOWED to open (pool + overflow + platform
pool + platform overflow per process); it is what Postgres must be able to accept, because under
load every pool fills. ``expected`` is what the read path actually holds: the tenant pool saturated,
the platform pool unopened (it is lazy and only staff routes use it), the worker at its derived
concurrency plus its reserve.

Limits are Azure's published defaults (learn.microsoft.com/azure/postgresql/configure-maintain/
concepts-limits, 2026-07): max connections, minus 15 reserved for replication and monitoring.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

# sku -> (max_connections, max user connections)
SKU_LIMITS = {
    "B_Standard_B1ms": (50, 35),
    "B_Standard_B2s": (429, 414),
    "B_Standard_B2ms": (859, 844),
    "GP_Standard_D2ds_v5": (859, 844),
    "GP_Standard_D4ds_v5": (1718, 1703),
}
# Built-in PgBouncer is not available on Burstable (same page, "PgBouncer" limitation).
PGBOUNCER_BUILTIN = {sku: not sku.startswith("B_") for sku in SKU_LIMITS}

WORKER_POOL_RESERVE = 5  # nexus/workers/worker.py POOL_RESERVE


@dataclass(frozen=True)
class Shape:
    name: str
    sku: str
    pool: int          # NEXUS_DB_POOL_SIZE
    overflow: int      # NEXUS_DB_MAX_OVERFLOW
    platform_pool: int = 2
    platform_overflow: int = 3
    uvicorn_workers: int = 1
    app_min: int = 1
    app_max: int = 2
    worker_replicas: int = 1

    @property
    def ceiling_per_process(self) -> int:
        return self.pool + self.overflow + self.platform_pool + self.platform_overflow

    @property
    def expected_per_app_process(self) -> int:
        return self.pool + self.overflow

    @property
    def worker_concurrency(self) -> int:
        return max(1, self.pool + self.overflow - WORKER_POOL_RESERVE)

    @property
    def expected_worker(self) -> int:
        # In-flight handlers each hold a session, plus the reserve (scheduler lock, state metrics,
        # dead-letter writer) — i.e. the pool, saturated.
        return self.pool + self.overflow


STAGING = Shape("staging", "B_Standard_B1ms", pool=5, overflow=5, app_max=2)
PRODUCTION = Shape("production", "B_Standard_B1ms", pool=5, overflow=5, app_max=3)

# Boot-time migration (deploy/entrypoint.sh runs `alembic upgrade head` as the owner role) holds one
# connection on the NEW revision while the old one is still serving.
MIGRATION_CONNECTIONS = 1


def rows(shape: Shape) -> list[dict]:
    limit_total, limit_user = SKU_LIMITS[shape.sku]
    procs = shape.uvicorn_workers
    out = []

    def add(label: str, app_replicas: int, rollout: bool) -> None:
        app_procs = app_replicas * procs * (2 if rollout else 1)
        ceiling = (app_procs * shape.ceiling_per_process
                   + shape.worker_replicas * shape.ceiling_per_process
                   + (MIGRATION_CONNECTIONS if rollout else 0))
        expected = (app_procs * shape.expected_per_app_process
                    + shape.worker_replicas * shape.expected_worker
                    + (MIGRATION_CONNECTIONS if rollout else 0))
        out.append({
            "scenario": label,
            "app_processes": app_procs,
            "ceiling": ceiling,
            "expected": expected,
            "user_limit": limit_user,
            "ceiling_verdict": "OK" if ceiling <= limit_user else "EXCEEDS",
            "expected_verdict": "OK" if expected <= limit_user else "EXCEEDS",
        })

    add(f"steady, {shape.app_min} replica", shape.app_min, rollout=False)
    add(f"rollout at {shape.app_min} replica (old+new)", shape.app_min, rollout=True)
    if shape.app_max > shape.app_min:
        add(f"scaled out to app_max={shape.app_max}", shape.app_max, rollout=False)
        add(f"rollout while at app_max={shape.app_max}", shape.app_max, rollout=True)
    return out


def headroom_replicas(shape: Shape) -> int:
    """Most app replicas whose *ceiling* fits beside the worker, outside a rollout."""
    _, limit_user = SKU_LIMITS[shape.sku]
    free = limit_user - shape.worker_replicas * shape.ceiling_per_process
    return max(0, free // (shape.uvicorn_workers * shape.ceiling_per_process))


def markdown(shape: Shape) -> str:
    total, user = SKU_LIMITS[shape.sku]
    lines = [
        f"**{shape.name}** — `{shape.sku}` ({total} max / **{user} user** connections), "
        f"pools {shape.pool}+{shape.overflow} (+{shape.platform_pool}+{shape.platform_overflow} "
        f"platform) = {shape.ceiling_per_process}/process, {shape.uvicorn_workers} uvicorn "
        f"worker, app {shape.app_min}..{shape.app_max}, worker concurrency "
        f"{shape.worker_concurrency}. Built-in PgBouncer: "
        f"{'available' if PGBOUNCER_BUILTIN[shape.sku] else 'not available on Burstable'}.",
        "",
        "| scenario | app processes | ceiling | expected | vs user limit |",
        "|---|---|---|---|---|",
    ]
    for r in rows(shape):
        lines.append(
            f"| {r['scenario']} | {r['app_processes']} | {r['ceiling']} ({r['ceiling_verdict']}) "
            f"| {r['expected']} ({r['expected_verdict']}) | {r['user_limit']} |"
        )
    lines.append("")
    lines.append(f"Replicas whose ceiling fits beside the worker: **{headroom_replicas(shape)}**.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Postgres connection budget")
    parser.add_argument("--sku", choices=sorted(SKU_LIMITS))
    parser.add_argument("--pool", type=int)
    parser.add_argument("--overflow", type=int)
    parser.add_argument("--platform-pool", type=int)
    parser.add_argument("--platform-overflow", type=int)
    parser.add_argument("--app-max", type=int)
    args = parser.parse_args(argv)
    shapes = [STAGING, PRODUCTION]
    overrides = (args.sku, args.pool, args.overflow, args.platform_pool, args.platform_overflow,
                 args.app_max)
    if any(v is not None for v in overrides):
        def pick(value, default):
            return default if value is None else value

        shapes = [
            Shape(
                f"what-if ({base.name})", args.sku or base.sku,
                pool=pick(args.pool, base.pool),
                overflow=pick(args.overflow, base.overflow),
                platform_pool=pick(args.platform_pool, base.platform_pool),
                platform_overflow=pick(args.platform_overflow, base.platform_overflow),
                app_max=pick(args.app_max, base.app_max),
            )
            for base in shapes
        ]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n\n".join(markdown(s) for s in shapes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
