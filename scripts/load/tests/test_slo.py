import json

import pytest

import slo
from common import ConfigError

VALID = {
    "profile": "baseline",
    "profiles": {
        "baseline": {"api": {"p95_ms": 500, "p99_ms": 1500, "error_rate_pct": 1.0}, "availability_pct": 99.5,
                     "queue_lag_minutes": 10, "alert_window": {"bucket": "1m", "consecutive": 5}},
        "strict": {"api": {"p95_ms": 300, "p99_ms": 1000, "error_rate_pct": 0.5}, "availability_pct": 99.9,
                   "queue_lag_minutes": 5, "alert_window": {"bucket": "1m", "consecutive": 3},
                   "routes": {"GET /api/accounts": {"p95_ms": 250}}},
    },
    "routes": {"GET /api/inbox": {"p95_ms": 400}},
}


def test_fixture_export_is_marked_and_matches_the_owner_decision():
    out = slo.export(path=slo.FIXTURE_SLO)
    assert out["is_fixture"] is True
    assert out["api"] == {"p95_ms": 500, "p99_ms": 1500, "error_rate_pct": 1.0}
    assert slo.export("strict", path=slo.FIXTURE_SLO)["api"]["p95_ms"] == 300


def test_profile_routes_override_top_level_routes():
    resolved = slo.resolve(VALID, "strict")
    assert resolved["routes"] == {"GET /api/inbox": {"p95_ms": 400}, "GET /api/accounts": {"p95_ms": 250}}


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.__setitem__("profile", "turbo"), "is not defined"),
    (lambda d: d["profiles"]["baseline"]["api"].pop("p99_ms"), "missing `p99_ms`"),
    (lambda d: d["profiles"]["baseline"]["api"].__setitem__("p99_ms", 100), "below p95_ms"),
    (lambda d: d["profiles"]["baseline"].pop("alert_window"), "alert_window"),
    (lambda d: d["routes"].__setitem__("accounts", {"p95_ms": 1}), "is not"),
    (lambda d: d["routes"].__setitem__("GET /api/x", {"latency": 1}), "unknown keys"),
])
def test_bad_slo_files_fail_loudly(mutate, message):
    data = json.loads(json.dumps(VALID))
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        slo.resolve(data, None)


def test_foundation_export_is_used_verbatim(tmp_path, monkeypatch):
    exported = {"profile": "strict", "api": {"p95_ms": 300, "p99_ms": 1000, "error_rate_pct": 0.5},
                "availability_pct": 99.9, "queue_lag_minutes": 5, "alert_window": {"bucket": "1m", "consecutive": 3}}
    path = tmp_path / "slo.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    monkeypatch.setenv("LT_SLO_EXPORT", str(path))
    out = slo.export()
    assert out["is_fixture"] is False and out["profile"] == "strict"
    with pytest.raises(ConfigError, match="was requested"):
        slo.export("baseline")
