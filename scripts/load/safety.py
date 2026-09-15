"""Staging safety: target allowlists, run windows, hard caps and live guards.

Everything here is a pure function of configuration and observations so it can be unit-tested
without a network. ``run.py`` wires it to real clocks, HTTP probes and containers.

The asymmetry that shapes every default: a refused run costs someone a re-schedule; an unrefused
one on shared staging costs every developer testing a deploy that evening.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from urllib.parse import urlparse

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SafetyError(RuntimeError):
    """The run must not start (or must stop). The message says why and what would allow it."""


# ------------------------------------------------------------------------------ targets
def check_target(env_name: str, env: dict, forbidden_hosts: list[str], base_url: str) -> str:
    """Return the target host, or raise if it is forbidden or not allowlisted for ``env_name``.

    Forbidden is checked FIRST and as a prefix, so a production host cannot be let through by also
    listing it as allowed, and a new production revision FQDN is caught by its stem.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise SafetyError(f"target {base_url!r} is not an http(s) URL")
    for stem in forbidden_hosts:
        stem = stem.lower()
        if host == stem or host.startswith(stem):
            raise SafetyError(
                f"target host {host!r} matches forbidden host {stem!r}. Load tests never run "
                "against production; there is no flag for this."
            )
    allowed = [h.lower() for h in env.get("allowed_hosts", [])]
    if host not in allowed:
        raise SafetyError(
            f"target host {host!r} is not in environments.{env_name}.allowed_hosts {allowed}"
        )
    return host


# ------------------------------------------------------------------------------ run window
def zone(name: str) -> tzinfo:
    """IANA zone, with a fixed-offset fallback for the one we rely on.

    Windows Pythons ship without the tz database unless ``tzdata`` is installed; Asia/Kolkata has
    had no DST since 1945, so +05:30 is exact rather than an approximation.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Kolkata":
            return timezone(timedelta(hours=5, minutes=30), "IST")
        raise SafetyError(f"time zone {name!r} unavailable; pip install tzdata")


def _hhmm(value: str) -> time:
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


def _blocked_spans(window: dict, tz: tzinfo, around: datetime, days: int = 9):
    """Blocked [start, end) datetimes for the days surrounding ``around``."""
    blocked = window.get("blocked") or {}
    names = [d.lower() for d in blocked.get("days", [])]
    start_t, end_t = _hhmm(blocked.get("start", "00:00")), _hhmm(blocked.get("end", "00:00"))
    base = around.astimezone(tz).date() - timedelta(days=1)
    for offset in range(days):
        day = base + timedelta(days=offset)
        if DAYS[day.weekday()] not in names:
            continue
        start = datetime.combine(day, start_t, tz)
        end = datetime.combine(day, end_t, tz)
        if end <= start:  # a span that crosses midnight
            end += timedelta(days=1)
        yield start, end


def window_verdict(window: dict, tz_name: str, start: datetime, duration_s: float
                   ) -> tuple[bool, str]:
    """Whether a run starting at ``start`` for ``duration_s`` stays outside every blocked span."""
    if not window.get("enforce"):
        return True, "run window not enforced for this environment"
    tz = zone(tz_name)
    end = start + timedelta(seconds=duration_s)
    for span_start, span_end in _blocked_spans(window, tz, start):
        if start < span_end and end > span_start:
            nxt = next_allowed_start(window, tz_name, start, duration_s)
            return False, (
                f"run {start.astimezone(tz):%a %H:%M}-{end.astimezone(tz):%H:%M} {tz_name} overlaps "
                f"the blocked span {span_start:%a %H:%M}-{span_end:%H:%M} (Indian working hours). "
                f"Next start that fits: {nxt.astimezone(tz):%a %Y-%m-%d %H:%M} {tz_name}."
            )
    return True, f"outside blocked hours ({start.astimezone(tz):%a %H:%M} {tz_name})"


def next_allowed_start(window: dict, tz_name: str, after: datetime, duration_s: float) -> datetime:
    """Earliest start >= ``after`` (in 5-minute steps) whose whole run avoids blocked spans."""
    candidate = after.replace(second=0, microsecond=0)
    for _ in range(12 * 24 * 8):
        candidate += timedelta(minutes=5)
        ok, _ = window_verdict({**window, "enforce": False}, tz_name, candidate, duration_s)
        clash = any(
            candidate < s_end and candidate + timedelta(seconds=duration_s) > s_start
            for s_start, s_end in _blocked_spans(window, zone(tz_name), candidate)
        )
        if ok and not clash:
            return candidate
    raise SafetyError("no allowed start within 8 days; the run is longer than every open window")


# ------------------------------------------------------------------------------ caps
CAP_KEYS = ("max_vus", "max_duration_s", "max_rps", "max_users_per_sec")


def check_caps(envelope: dict, caps: dict) -> list[str]:
    """Violations of the environment's hard caps by a plan's envelope. Empty means allowed."""
    problems = []
    pairs = (
        ("peak_vus", "max_vus", "virtual users (estimated peak concurrency)"),
        ("duration_s", "max_duration_s", "seconds of run time, including graceful stop"),
        ("est_peak_rps", "max_rps", "requests per second (estimated peak)"),
        ("peak_users_per_sec", "max_users_per_sec", "new users per second"),
    )
    for measured, cap, what in pairs:
        if cap in caps and envelope.get(measured, 0) > caps[cap]:
            problems.append(
                f"{envelope[measured]:g} {what} exceeds {cap}={caps[cap]:g}"
            )
    return problems


# ------------------------------------------------------------------------------ live guards
@dataclass
class Sample:
    ok: bool
    latency_ms: float


@dataclass
class CanaryWindow:
    """Rolling window of the runner's own canary probes against the target.

    Independent of the load tool on purpose: if a tool's in-script guard is broken, misconfigured
    or simply the thing being overwhelmed, the runner still sees staging from the outside.
    """

    size: int
    fail_ratio: float
    p95_ms: float
    samples: list[Sample] = field(default_factory=list)

    def add(self, ok: bool, latency_ms: float) -> None:
        self.samples.append(Sample(ok, latency_ms))
        del self.samples[: -self.size]

    def breach(self) -> str | None:
        if len(self.samples) < self.size:
            return None
        failed = sum(1 for s in self.samples if not s.ok)
        if failed / len(self.samples) >= self.fail_ratio:
            return (f"canary: {failed}/{len(self.samples)} readiness probes failed "
                    f"(guard {self.fail_ratio:.0%})")
        latencies = sorted(s.latency_ms for s in self.samples if s.ok)
        if latencies:
            median = latencies[len(latencies) // 2]
            if median > self.p95_ms:
                return (f"canary: median readiness latency {median:.0f} ms over the last "
                        f"{len(self.samples)} probes exceeds guard {self.p95_ms:.0f} ms")
        return None


def tool_guard_breach(total: int, failed: int, over_p95: int, guards: dict) -> str | None:
    """The in-tool guard, shared semantics for k6 and Gatling (both implement this formula).

    ``over_p95`` is the count of requests slower than ``guards.p95_ms``. p95 > X exactly when more
    than 5% of requests exceed X, so a counter answers a percentile question without keeping every
    latency in memory.
    """
    if total < guards.get("min_requests", 50):
        return None
    if failed * 100.0 / total >= guards["error_rate_pct"]:
        return f"error rate {failed * 100.0 / total:.1f}% >= guard {guards['error_rate_pct']}%"
    if over_p95 / total > 0.05:
        return f"p95 above guard {guards['p95_ms']} ms ({over_p95}/{total} requests slower)"
    return None


def ceil_int(value: float) -> int:
    return int(math.ceil(value - 1e-9))
