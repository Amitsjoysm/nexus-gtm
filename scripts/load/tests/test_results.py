import json

import pytest

import results

SLO = {"profile": "baseline", "is_fixture": True, "api": {"p95_ms": 500, "p99_ms": 1500, "error_rate_pct": 1.0},
       "routes": {"GET /api/accounts/{account_id}": {"p95_ms": 200}}}


def summary(started: str, p95: float, *, profile="load", tool="k6", env="staging", passed=True) -> dict:
    return {
        "schema": results.RESULT_SCHEMA, "started_at": started, "profile": profile,
        "tool": {"name": tool}, "environment": {"name": env},
        "plan": {"kind": "ramping_vus", "envelope": {"peak_vus": 20}},
        "metrics": {"requests": 1000, "rps": 9.5, "p50_ms": 40, "p95_ms": p95, "p99_ms": 900, "error_rate_pct": 0.1},
        "slo_verdict": {"passed": passed, "is_fixture": True}, "guard": {"aborted": False},
    }


def test_verdict_grades_overall_journey_and_route_limits():
    good = {"requests": 10, "p95_ms": 300, "p99_ms": 900, "error_rate_pct": 0.2,
            "by_journey": {"rep_daily": {"p95_ms": 450}}, "by_request": {"GET /api/accounts/:account_id": {"p95_ms": 150}}}
    assert results.slo_verdict(good, SLO)["passed"] is True
    bad = {**good, "error_rate_pct": 1.0, "by_journey": {"rep_daily": {"p95_ms": 700}},
           "by_request": {"GET /api/accounts/:account_id": {"p95_ms": 260}}}
    breaches = results.slo_verdict(bad, SLO)["breaches"]
    assert any("error rate" in b for b in breaches)
    assert any("rep_daily p95" in b for b in breaches)
    assert any("GET /api/accounts/:account_id p95" in b for b in breaches)
    assert results.slo_verdict({"requests": 0}, SLO)["passed"] is False


def test_trend_table_compares_against_the_previous_run_of_the_same_kind(tmp_path):
    rdir = tmp_path / "load-results"
    rdir.mkdir()
    for name, s in (("a.json", summary("2026-09-15T16:00:00+00:00", 400)),
                    ("b.json", summary("2026-09-16T16:00:00+00:00", 500)),
                    ("c.json", summary("2026-09-16T17:00:00+00:00", 900, profile="stress", tool="gatling"))):
        (rdir / name).write_text(json.dumps(s), encoding="utf-8")
    doc = tmp_path / "load.md"
    doc.write_text(f"# x\n{results.TREND_BEGIN}\nold\n{results.TREND_END}\ntail\n", encoding="utf-8")
    assert results.update_trend(doc, rdir) is True
    text = doc.read_text(encoding="utf-8")
    assert "+25%" in text                     # b vs a: same env/tool/profile
    assert text.count("| staging |") == 3
    assert "Reference — the only earlier measurement" in text and text.endswith("tail\n")
    assert results.update_trend(doc, rdir) is False


def test_missing_markers_are_an_error(tmp_path):
    doc = tmp_path / "load.md"
    doc.write_text("no markers", encoding="utf-8")
    with pytest.raises(ValueError, match="markers"):
        results.update_trend(doc, tmp_path)


def test_result_paths_follow_the_brief_and_never_overwrite(tmp_path):
    s = summary("2026-09-15T16:00:00+00:00", 400)
    first = results.write_result(s, tmp_path)
    assert first.name == "2026-09-15-k6-load.json"
    second = results.write_result(s, tmp_path)
    assert second.name == "2026-09-15-k6-load-2130.json"
