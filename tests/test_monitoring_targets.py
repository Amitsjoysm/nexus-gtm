# tests/test_monitoring_targets.py
"""A scrape target must identify WHICH replica answered it.

Found by starting the monitoring overlay for the first time during a readiness audit. The stack
came up clean — three targets `up`, thirteen alert rules loaded, every one `health=ok` and
`inactive`. It was still substantially blind.

`app` runs `deploy.replicas: 2` and Prometheus had one static target, `app:8000`. Docker's
embedded DNS round-robins that name, so each scrape hit whichever replica DNS returned and every
sample was labelled `instance="app:8000"` either way. Measured over ten minutes: two distinct
series carrying the SAME instance label, one reading 1 and the other 3.

Three failures, from one missing label:

  * A counter that alternates between replicas appears to DECREASE, and `rate()` reads a decrease
    as a counter reset. Every rate over an app counter is wrong, and `HighServerErrorRate` is a
    rate.
  * Roughly half of each replica's samples are discarded as duplicates of the other's.
  * `up{job="nexus-app"} == 0` cannot detect ONE replica failing, because DNS keeps returning the
    survivor. That is exactly the failure two replicas exist to survive, and the alert named
    AppDown reads all-clear for the whole of it.

`dns_sd_configs` with `type: A` creates one target per resolved address. Verified live: two
targets, `172.18.0.4:8000` and `172.18.0.7:8000`, with the counters separating onto the replica
that actually recorded them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROM = ROOT / "deploy" / "monitoring" / "prometheus.yml"
COMPOSE = ROOT / "deploy" / "docker-compose.prod.yml"


def _app_replicas() -> int:
    m = re.search(r"^\s*replicas:\s*(\d+)", COMPOSE.read_text(encoding="utf-8"), re.M)
    return int(m.group(1)) if m else 1


def _job_block(name: str) -> str:
    body = PROM.read_text(encoding="utf-8")
    m = re.search(rf"^  - job_name: {name}$(.*?)(?=^  - job_name:|\Z)", body, re.M | re.S)
    assert m, f"job {name!r} not found in prometheus.yml"
    return m.group(1)


def test_a_replicated_service_is_not_scraped_as_one_static_target():
    """The bug. A static target against a round-robin DNS name collapses every replica onto one
    instance label."""
    if _app_replicas() < 2:
        return  # a single replica is unambiguous; nothing to distinguish.

    block = _job_block("nexus-app")
    assert "dns_sd_configs" in block, (
        "the app is replicated but Prometheus scrapes it as a static target, so every replica is "
        "labelled identically and one replica failing cannot be detected"
    )
    assert not re.search(r"static_configs", block), (
        "the app job still carries a static target alongside service discovery"
    )
    assert re.search(r"type:\s*A\b", block), (
        "DNS discovery must use A records; the default SRV lookup finds nothing for a compose "
        "service name"
    )


def test_the_worker_stays_a_static_target():
    """It is a single process with its own port. Guarding against an over-broad edit that converts
    every job and breaks the one that was correct."""
    assert "static_configs" in _job_block("nexus-worker")


def test_availability_is_alerted_per_instance():
    """`up == 0` is only meaningful once each replica carries its own instance label; the rule and
    the discovery are two halves of one property."""
    rules = (ROOT / "deploy" / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    m = re.search(r"alert:\s*AppDown(.*?)(?=- alert:|\Z)", rules, re.S)
    assert m, "the AppDown alert is gone"
    assert re.search(r'up\{job="nexus-app"\}\s*==\s*0', m.group(1)), (
        "AppDown no longer tests scrape availability"
    )
