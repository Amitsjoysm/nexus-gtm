from datetime import datetime, timedelta, timezone

import pytest

from safety import CanaryWindow, SafetyError, check_caps, check_target, next_allowed_start, tool_guard_breach, window_verdict

IST = timezone(timedelta(hours=5, minutes=30))
STAGING = {"allowed_hosts": ["gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io"]}
FORBIDDEN = ["gtm.infojoy.com", "gtm-prod-app"]
WINDOW = {"enforce": True, "blocked": {"days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                                        "start": "08:00", "end": "21:00"}}


def test_production_is_refused_even_if_allowlisted():
    env = {"allowed_hosts": ["gtm.infojoy.com"]}
    with pytest.raises(SafetyError, match="forbidden"):
        check_target("staging", env, FORBIDDEN, "https://gtm.infojoy.com")
    with pytest.raises(SafetyError, match="forbidden"):
        check_target("staging", env, FORBIDDEN, "https://gtm-prod-app--rev7.eastus2.azurecontainerapps.io")


def test_unlisted_and_non_http_targets_are_refused():
    with pytest.raises(SafetyError, match="not in environments.staging.allowed_hosts"):
        check_target("staging", STAGING, FORBIDDEN, "https://example.com")
    with pytest.raises(SafetyError, match="not an http"):
        check_target("staging", STAGING, FORBIDDEN, "ftp://x")
    assert check_target("staging", STAGING, FORBIDDEN, STAGING_URL) == STAGING["allowed_hosts"][0]


STAGING_URL = "https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-09-14 is a Monday.
    return datetime(2026, 9, 14 + day, hour, minute, tzinfo=IST)


def test_window_blocks_indian_working_hours_and_allows_evenings_and_sunday():
    assert window_verdict(WINDOW, "Asia/Kolkata", at(1, 12), 600)[0] is False      # Tue noon
    assert window_verdict(WINDOW, "Asia/Kolkata", at(1, 21, 5), 1800)[0] is True   # Tue 21:05
    assert window_verdict(WINDOW, "Asia/Kolkata", at(6, 12), 3600)[0] is True      # Sunday
    assert window_verdict({"enforce": False}, "Asia/Kolkata", at(1, 12), 600)[0] is True


def test_a_run_that_would_run_into_the_morning_is_refused_with_the_next_slot():
    ok, reason = window_verdict(WINDOW, "Asia/Kolkata", at(2, 7, 30), 45 * 60)
    assert not ok and "Next start that fits" in reason
    nxt = next_allowed_start(WINDOW, "Asia/Kolkata", at(2, 7, 30), 45 * 60)
    assert nxt.astimezone(IST).hour == 21


def test_caps_report_every_violation():
    caps = {"max_vus": 60, "max_duration_s": 3600, "max_rps": 60, "max_users_per_sec": 5}
    assert check_caps({"peak_vus": 60, "duration_s": 3600, "est_peak_rps": 59, "peak_users_per_sec": 5}, caps) == []
    problems = check_caps({"peak_vus": 61, "duration_s": 4000, "est_peak_rps": 70, "peak_users_per_sec": 6}, caps)
    assert len(problems) == 4


def test_canary_trips_on_failures_and_on_latency_only_after_a_full_window():
    w = CanaryWindow(size=4, fail_ratio=0.5, p95_ms=1000)
    for ok in (False, False, True):
        w.add(ok, 50)
    assert w.breach() is None
    w.add(True, 50)
    assert "2/4 readiness probes failed" in w.breach()
    slow = CanaryWindow(size=3, fail_ratio=0.5, p95_ms=1000)
    for _ in range(3):
        slow.add(True, 2500)
    assert "median readiness latency" in slow.breach()


def test_tool_guard_formula():
    guards = {"error_rate_pct": 5, "p95_ms": 3000, "min_requests": 50}
    assert tool_guard_breach(40, 40, 0, guards) is None            # not enough evidence yet
    assert "error rate" in tool_guard_breach(100, 5, 0, guards)
    assert tool_guard_breach(100, 4, 5, guards) is None            # exactly 5% slow is p95 == guard
    assert "p95 above guard" in tool_guard_breach(100, 0, 6, guards)
