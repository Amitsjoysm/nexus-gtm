"""Background probes that run beside the load tool: the canary guard and the worker-health sampler.

Neither adds meaningful load. The canary is one GET /ready every ``canary_interval_s``; the worker
probe reads gauges the worker already exports (nexus/workers/state_metrics.py) through Prometheus —
it never touches the worker, which has no ingress and serves no requests.
"""
from __future__ import annotations

import base64
import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

from preflight import Response, request
from safety import CanaryWindow


class Canary(threading.Thread):
    """Watches the target from outside the load tool and calls ``on_breach`` once if a guard trips."""

    def __init__(self, host_url: str, headers: dict, guards: dict, on_breach: Callable[[str], None],
                 *, insecure: bool = False, probe: Callable[..., Response] = request) -> None:
        super().__init__(name="lt-canary", daemon=True)
        self.url = f"{host_url}/ready"
        self.headers = headers
        self.interval = float(guards["canary_interval_s"])
        self.window = CanaryWindow(int(guards["canary_window"]), float(guards["canary_fail_ratio"]),
                                   float(guards["p95_ms"]))
        self.on_breach = on_breach
        self.insecure = insecure
        self.probe = probe
        self.stopped = threading.Event()
        self.breach: str | None = None
        self.samples = 0
        self.failures = 0

    def tick(self) -> None:
        r = self.probe("GET", self.url, headers=self.headers, insecure=self.insecure,
                       timeout=min(10.0, self.interval))
        ok = r.status == 200
        self.samples += 1
        self.failures += 0 if ok else 1
        self.window.add(ok, r.latency_ms)
        breach = self.window.breach()
        if breach and not self.breach:
            self.breach = breach
            self.on_breach(breach)

    def run(self) -> None:
        while not self.stopped.wait(self.interval):
            self.tick()

    def stop(self) -> dict:
        self.stopped.set()
        return {"samples": self.samples, "failures": self.failures, "breach": self.breach}


QUERIES = {
    "nexus_queue_depth": "sum(nexus_queue_depth)",
    "nexus_dead_letter_jobs": "sum(nexus_dead_letter_jobs)",
    "nexus_jobs_total": "sum(rate(nexus_jobs_total[5m]))",
}


class WorkerProbe(threading.Thread):
    """Samples worker gauges from Prometheus (local) or Grafana Cloud (staging, once it exists)."""

    def __init__(self, config: dict, metrics: list[str], interval_s: float, *,
                 environ: dict | os._Environ = os.environ,
                 probe: Callable[..., Response] = request) -> None:
        super().__init__(name="lt-worker-probe", daemon=True)
        self.config = config or {"source": "none"}
        self.metrics = [m for m in metrics if m in QUERIES]
        self.interval = float(interval_s)
        self.environ = environ
        self.probe = probe
        self.stopped = threading.Event()
        self.samples: list[dict] = []
        self.reason: str | None = None
        self.url, self.headers = self._resolve()

    def _resolve(self) -> tuple[str | None, dict]:
        source = self.config.get("source", "none")
        if source == "prometheus":
            return self.config.get("url"), {}
        if source == "grafana_prometheus":
            url = self.environ.get(self.config.get("url_env", ""))
            user = self.environ.get(self.config.get("user_env", ""))
            token = self.environ.get(self.config.get("token_env", ""))
            if not (url and token):
                self.reason = (f"{self.config.get('url_env')} / {self.config.get('token_env')} unset: "
                               "Grafana Cloud metrics are not wired yet (Operations task); the staging "
                               "worker has no ingress to scrape directly")
                return None, {}
            auth = f"{user}:{token}" if user else token
            scheme = "Basic " + base64.b64encode(auth.encode()).decode() if user else f"Bearer {token}"
            return url, {"Authorization": scheme}
        self.reason = f"worker probe source {source!r} is not configured"
        return None, {}

    def sample(self) -> dict:
        row: dict = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for metric in self.metrics:
            query = urllib.parse.quote(QUERIES[metric])
            r = self.probe("GET", f"{self.url.rstrip('/')}/api/v1/query?query={query}",
                           headers=self.headers, timeout=10.0)
            body = r.json() or {}
            result = (body.get("data") or {}).get("result") or []
            # An empty vector is "not measured", never 0: a missing gauge must not read as healthy.
            row[metric] = round(float(result[0]["value"][1]), 3) if r.status == 200 and result else None
        self.samples.append(row)
        return row

    def run(self) -> None:
        if not self.url:
            return
        self.sample()
        while not self.stopped.wait(self.interval):
            self.sample()

    def stop(self) -> dict:
        self.stopped.set()
        if self.url and not self.samples:
            self.sample()
        measured = [s for s in self.samples if any(s.get(m) is not None for m in self.metrics)]
        summary: dict = {
            "source": self.config.get("source", "none"),
            "status": "measured" if measured else "unmeasured",
            "reason": self.reason if not measured else None,
            "samples": self.samples,
        }
        for metric in self.metrics:
            values = [s[metric] for s in measured if s.get(metric) is not None]
            if values:
                summary[metric] = {"first": values[0], "last": values[-1], "max": max(values)}
        return summary


def wait_until(predicate: Callable[[], bool], timeout_s: float, step_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step_s)
    return predicate()
