"""Launch k6 and Gatling in Docker, supervise them, and normalise what they report.

Both tools run in containers so the same command works on a laptop and on a Microsoft-hosted ADO
agent, with pinned versions and no local install. Secrets (tokens, persona passwords) reach the
container as ``-e NAME`` with the value in the process environment — never on the command line,
where ``ps`` and CI logs would show them.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from common import REPO_ROOT

K6_IMAGE = "grafana/k6:2.2.0"
NODE_IMAGE = "node:24-bookworm"
GATLING_CLI = "@gatling.io/cli 3.15.105"
GATLING_HOME_VOLUME = "nexus-loadtest-gatling-home"   # the ~400 MB Gatling runtime bundle
NPM_CACHE_VOLUME = "nexus-loadtest-npm-cache"
GATLING_SETUP_ALLOWANCE_S = 600                        # first run downloads the bundle + npm ci


@dataclass
class Launch:
    tool: str
    argv: list[str]
    env: dict[str, str]
    container: str
    timeout_s: float


def container_name(run_id: str) -> str:
    return f"nexus-lt-{run_id}"


def _common(run_id: str, run_dir: Path, env_cfg: dict) -> list[str]:
    argv = ["docker", "run", "--rm", "--init", "--name", container_name(run_id)]
    if env_cfg.get("docker_network"):
        argv += ["--network", env_cfg["docker_network"]]
    return argv + ["-v", f"{REPO_ROOT}:/repo:ro", "-v", f"{run_dir}:/results"]


def _env_flags(env: dict[str, str]) -> list[str]:
    return [flag for name in sorted(env) for flag in ("-e", name)]


def k6_launch(run_id: str, run_dir: Path, env_cfg: dict, plan: dict, *, base_url: str,
              slo: dict, secrets: dict[str, str], environ: dict | os._Environ = os.environ) -> Launch:
    env = {
        "LT_REPO": "/repo", "LT_ENV": env_cfg["name"], "LT_BASE_URL": base_url, "LT_RUN_ID": run_id,
        "LT_PLAN_JSON": json.dumps(plan), "LT_SLO_JSON": json.dumps(slo), **secrets,
    }
    outputs: list[str] = []
    # Grafana Cloud (Operations task): k6 metrics via Prometheus remote-write, names k6_*.
    if environ.get("LT_PROM_RW_URL"):
        env.update({
            "K6_PROMETHEUS_RW_SERVER_URL": environ["LT_PROM_RW_URL"],
            "K6_PROMETHEUS_RW_USERNAME": environ.get("LT_PROM_RW_USER", ""),
            "K6_PROMETHEUS_RW_PASSWORD": environ.get("LT_PROM_RW_TOKEN", ""),
            "K6_PROMETHEUS_RW_TREND_STATS": "p(50),p(95),p(99),max",
            "K6_PROMETHEUS_RW_PUSH_INTERVAL": "30s",  # free tier: 1-minute resolution anyway
        })
        outputs = ["-o", "experimental-prometheus-rw"]
    argv = (_common(run_id, run_dir, env_cfg) + _env_flags(env) + [
        K6_IMAGE, "run", "--summary-export", "/results/k6-summary.json", *outputs,
        f"/repo/deploy/loadtest/k6/{plan['profile']}.js",
    ])
    return Launch("k6", argv, env, container_name(run_id), plan["max_duration_s"] + 180)


def gatling_launch(run_id: str, run_dir: Path, env_cfg: dict, plan: dict, *, base_url: str,
                   slo: dict, secrets: dict[str, str]) -> Launch:
    env = {
        "CI": "true", "LT_ENV": env_cfg["name"], "LT_BASE_URL": base_url, "LT_RUN_ID": run_id,
        "LT_PLAN_JSON": json.dumps(plan), "LT_SLO_JSON": json.dumps(slo), **secrets,
    }
    # Copy the project out of the read-only mount WITHOUT the host's node_modules (esbuild ships a
    # native binary per OS; Windows modules crash under Linux), keep the repo layout so the
    # catalogue import path resolves, and exec npx under tini so `docker stop` reaches Gatling.
    script = (
        "set -e; mkdir -p /w/deploy/loadtest; "
        "tar -C /repo/deploy/loadtest --exclude=node_modules --exclude=target -cf - gatling "
        "| tar -C /w/deploy/loadtest -xf -; "
        "ln -s /repo/quality /w/quality; cd /w/deploy/loadtest/gatling; "
        "npm ci --no-audit --no-fund --loglevel=error; "
        f"exec npx gatling run --typescript --simulation {plan['profile']} --non-interactive "
        "--results-folder /results/gatling"
    )
    argv = (_common(run_id, run_dir, env_cfg)
            + ["-v", f"{GATLING_HOME_VOLUME}:/root/.gatling", "-v", f"{NPM_CACHE_VOLUME}:/root/.npm"]
            + _env_flags(env) + [NODE_IMAGE, "sh", "-c", script])
    return Launch("gatling", argv, env, container_name(run_id),
                  plan["max_duration_s"] + GATLING_SETUP_ALLOWANCE_S)


def stop_container(name: str, grace_s: int = 30) -> None:
    subprocess.run(["docker", "stop", "-t", str(grace_s), name], capture_output=True, text=True,
                   timeout=grace_s + 30)


@dataclass
class Execution:
    returncode: int
    timed_out: bool
    duration_s: float
    markers: dict[str, list[str]] = field(default_factory=dict)


MARKERS = {
    "guard": re.compile(r"LT_GUARD_BREACH (.+)"),
    "bootstrap_failed": re.compile(r"LT_BOOTSTRAP_FAILED (.+)"),
    "k6_abort": re.compile(r"(thresholds on metrics .* crossed.*stopping test prematurely.*)"),
}
SAMPLE_PREFIX = "LT_SAMPLE\t"


def execute(launch: Launch, log_path: Path, *, printer: Callable[[str], None] = print,
            stop: Callable[[str], None] = stop_container) -> Execution:
    """Run the container, tee its output to ``log_path``, enforce the hard timeout."""
    started = time.monotonic()
    markers: dict[str, list[str]] = {k: [] for k in MARKERS}
    proc = subprocess.Popen(launch.argv, env={**os.environ, **launch.env}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    timed_out = threading.Event()

    def enforce_timeout() -> None:
        while proc.poll() is None:
            if time.monotonic() - started > launch.timeout_s:
                timed_out.set()
                printer(f"hard timeout {launch.timeout_s:.0f}s reached: stopping {launch.container}")
                stop(launch.container)
                return
            time.sleep(2)

    threading.Thread(target=enforce_timeout, daemon=True).start()
    with log_path.open("w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if line.startswith(SAMPLE_PREFIX):
                continue  # per-request samples go to the log only, not the console
            printer(line.rstrip("\n"))
            for key, pattern in MARKERS.items():
                match = pattern.search(line)
                if match:
                    markers[key].append(match.group(1).strip())
    proc.wait()
    return Execution(proc.returncode, timed_out.is_set(), round(time.monotonic() - started, 1), markers)


# ------------------------------------------------------------------------------ helpers
def _num(value) -> float | None:
    try:
        return None if value in (None, "-", "") else float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an ascending list."""
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(q / 100 * len(sorted_values)) - 1)]


# ------------------------------------------------------------------------------ k6 results
def parse_k6(summary_path: Path) -> dict:
    """Normalise a k6 ``--summary-export`` file.

    In that (legacy) format a threshold maps to ``true`` when it was CROSSED, and there is no run
    duration, so throughput is the counter's own ``rate``.
    """
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    metrics = data.get("metrics", {})
    duration = metrics.get("http_req_duration{kind:api}") or {}
    failed = metrics.get("http_req_failed{kind:api}") or {}
    reqs = metrics.get("http_reqs{kind:api}") or {}
    requests = int(reqs.get("count") or 0)

    by_request = {}
    for key, value in metrics.items():
        match = re.fullmatch(r"http_req_duration\{name:(.+)\}", key)
        if not match:
            continue
        name = match.group(1)
        r_failed = metrics.get(f"http_req_failed{{name:{name}}}") or {}
        by_request[name] = {
            "requests": int(value.get("count") or 0),
            "p95_ms": _round(_num(value.get("p(95)"))),
            "p99_ms": _round(_num(value.get("p(99)"))),
            "max_ms": _round(_num(value.get("max"))),
            "error_rate_pct": _round((_num(r_failed.get("value")) or 0) * 100, 2),
        }

    by_journey = {}
    for key, value in metrics.items():
        match = re.fullmatch(r"http_req_duration\{journey:([a-z0-9_]+)\}", key)
        if not match:
            continue
        jid = match.group(1)
        j_failed = metrics.get(f"http_req_failed{{journey:{jid}}}") or {}
        by_journey[jid] = {
            "requests": int((metrics.get(f"http_reqs{{journey:{jid}}}") or {}).get("count") or 0),
            "p95_ms": _round(_num(value.get("p(95)"))),
            "error_rate_pct": _round((_num(j_failed.get("value")) or 0) * 100, 2),
        }
    thresholds = {
        key: value["thresholds"] for key, value in metrics.items() if value.get("thresholds")
    }

    def count(name: str) -> int:
        return int((metrics.get(name) or {}).get("count") or 0)

    checks = metrics.get("checks") or {}
    return {
        "requests": requests,
        "rps": _round(_num(reqs.get("rate"))),
        "p50_ms": _round(_num(duration.get("med"))),
        "p90_ms": _round(_num(duration.get("p(90)"))),
        "p95_ms": _round(_num(duration.get("p(95)"))),
        "p99_ms": _round(_num(duration.get("p(99)"))),
        "max_ms": _round(_num(duration.get("max"))),
        "error_rate_pct": _round((_num(failed.get("value")) or 0) * 100, 2),
        "checks_pass_pct": _round((_num(checks.get("value")) or 0) * 100, 2) if checks else None,
        "peak_vus": int((metrics.get("vus_max") or {}).get("max") or 0) or None,
        "by_journey": by_journey,
        "by_request": by_request,
        "percentile_method": "k6 trend over all API requests (per journey: over that journey's requests)",
        "counters": {
            "skipped_steps": count("lt_skipped_steps"),
            "logins": count("lt_logins"),
            "reauth_retries": count("lt_reauth_retries"),
            "paid_calls": count("lt_paid_calls"),
        },
        "tool_thresholds_crossed": thresholds,
    }


# ------------------------------------------------------------------------------ Gatling results
SAMPLE = re.compile(r"^LT_SAMPLE\t(\d+)\t([a-z0-9_]+)\t([^\t]+)\t(\d+)\t([01])\t(\d*)\s*$")
# Greedy detail: Gatling's "Could not find stats matching assertion path List(a, b)" nests parens.
ASSERTION = re.compile(r"^(.+?) : (true|false) \((?:actual : )?(.*)\)\s*$")


def latest_gatling_report(results_dir: Path) -> Path | None:
    reports = sorted((p for p in results_dir.glob("*") if (p / "index.html").exists()),
                     key=lambda p: p.stat().st_mtime) if results_dir.exists() else []
    return reports[-1] if reports else None


def _summarise(samples: list[tuple[int, bool, float | None]]) -> dict:
    latencies = sorted(ms for _, _, ms in samples if ms is not None)
    failed = sum(1 for _, ok, _ in samples if not ok)
    return {
        "requests": len(samples),
        "p50_ms": percentile(latencies, 50),
        "p90_ms": percentile(latencies, 90),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "max_ms": latencies[-1] if latencies else None,
        "error_rate_pct": _round(failed * 100 / len(samples), 2) if samples else None,
    }


def parse_gatling(log_text: str, report_dir: Path | None) -> dict:
    """Metrics from the simulation's own per-request samples (bootstrap logins are not sampled)."""
    # rstrip, not strip: a sample with no response time (a connection error) ends in a tab.
    rows = [m for line in log_text.splitlines() if (m := SAMPLE.match(line.rstrip("\r\n")))]
    overall: list[tuple[int, bool, float | None]] = []
    per_journey: dict[str, list] = {}
    per_request: dict[str, list] = {}
    for m in rows:
        sample = (int(m.group(1)), m.group(5) == "1", _num(m.group(6)))
        overall.append(sample)
        per_journey.setdefault(m.group(2), []).append(sample)
        per_request.setdefault(m.group(3), []).append(sample)

    stats = _summarise(overall)
    span_s = (max(s[0] for s in overall) - min(s[0] for s in overall)) / 1000 if len(overall) > 1 else 0
    assertions = [
        {"assertion": m.group(1), "passed": m.group(2) == "true", "actual": m.group(3)}
        for line in log_text.splitlines() if (m := ASSERTION.match(line.strip()))
    ]
    return {
        **stats,
        "rps": _round(len(overall) / span_s) if span_s else None,
        "checks_pass_pct": None,
        "peak_vus": None,
        "by_journey": {j: {k: v for k, v in _summarise(s).items()
                           if k in ("requests", "p95_ms", "error_rate_pct")}
                       for j, s in sorted(per_journey.items())},
        "by_request": {r: {k: v for k, v in _summarise(s).items()
                           if k in ("requests", "p95_ms", "p99_ms", "max_ms", "error_rate_pct")}
                       for r, s in sorted(per_request.items())},
        "percentile_method": ("nearest-rank over every API request the simulation sampled "
                              "(bootstrap logins excluded)"),
        "counters": {},
        "tool_assertions": assertions,
        "report": str(report_dir) if report_dir else None,
    }
