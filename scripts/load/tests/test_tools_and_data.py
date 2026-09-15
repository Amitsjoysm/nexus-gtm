import json
import subprocess

import pytest

import capacity
import notify
import preflight
import testdata
import tools
from common import REPO_ROOT

CAT = {
    "auth": {"login_path": "/api/auth/login", "login_rate_limit": {"max": 10, "window_s": 60}},
    "personas": {"rep": {"role": "rep", "env_prefix": "LT_REP"},
                 "platform_admin": {"role": "platform_admin", "env_prefix": "LT_PLATFORM_ADMIN"}},
}
PLAN = {"profile": "smoke", "max_duration_s": 240}
ENV = {"name": "staging", "docker_network": None}


def test_launch_specs_never_put_secrets_on_the_command_line(tmp_path):
    secrets = {"LT_TOKENS_JSON": "TOKEN-SECRET", "LT_PERSONAS_JSON": "PASSWORD-SECRET"}
    for launch in (
        tools.k6_launch("lt-x", tmp_path, ENV, PLAN, base_url="https://s", slo={"api": {}}, secrets=secrets, environ={}),
        tools.gatling_launch("lt-x", tmp_path, ENV, {**PLAN, "profile": "stress"}, base_url="https://s",
                             slo={"api": {}}, secrets=secrets),
    ):
        joined = " ".join(launch.argv)
        assert "TOKEN-SECRET" not in joined and "PASSWORD-SECRET" not in joined
        assert "LT_TOKENS_JSON" in launch.argv and launch.env["LT_TOKENS_JSON"] == "TOKEN-SECRET"
        assert "--init" in launch.argv and ":/repo:ro" in joined


def test_k6_remote_write_only_when_configured(tmp_path):
    plain = tools.k6_launch("lt-x", tmp_path, ENV, PLAN, base_url="https://s", slo={}, secrets={}, environ={})
    assert "experimental-prometheus-rw" not in plain.argv
    rw = tools.k6_launch("lt-x", tmp_path, ENV, PLAN, base_url="https://s", slo={}, secrets={},
                         environ={"LT_PROM_RW_URL": "https://prom/api/prom/push"})
    assert "experimental-prometheus-rw" in rw.argv and rw.env["K6_PROMETHEUS_RW_TREND_STATS"]


def test_parse_k6_summary_export(tmp_path):
    summary = {"metrics": {
        "http_req_duration{kind:api}": {"med": 40.0, "p(90)": 90.0, "p(95)": 120.5, "p(99)": 300.0, "max": 900.0,
                                        "thresholds": {"p(95)<500": False}},
        "http_req_failed{kind:api}": {"passes": 1, "fails": 99, "value": 0.01},
        "http_reqs{kind:api}": {"count": 100, "rate": 2.5},
        "http_req_duration{journey:rep_daily}": {"p(95)": 110.0},
        "http_reqs{journey:rep_daily}": {"count": 60},
        "http_req_failed{journey:rep_daily}": {"value": 0.0},
        "http_req_duration{name:GET /api/accounts/:account_id}": {"count": 20, "p(95)": 80.0, "p(99)": 95.0, "max": 99.0},
        "http_req_failed{name:GET /api/accounts/:account_id}": {"value": 0.05},
        "vus_max": {"max": 20}, "checks": {"value": 0.99}, "lt_skipped_steps": {"count": 2},
    }}
    path = tmp_path / "k6-summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    m = tools.parse_k6(path)
    assert (m["requests"], m["rps"], m["p95_ms"], m["error_rate_pct"], m["peak_vus"]) == (100, 2.5, 120.5, 1.0, 20)
    assert m["by_journey"]["rep_daily"] == {"requests": 60, "p95_ms": 110.0, "error_rate_pct": 0.0}
    assert m["by_request"]["GET /api/accounts/:account_id"]["error_rate_pct"] == 5.0
    assert m["counters"]["skipped_steps"] == 2


def test_parse_gatling_samples_and_assertions(tmp_path):
    report = tmp_path / "jssimulation-1"
    report.mkdir()
    (report / "index.html").write_text("<html></html>", encoding="utf-8")
    lines = ["08:19:31 [INFO] noise that is not a sample",
             "Global: 95th percentile of response time is less than 500.0 : false (actual : 712.0)",
             "manager_review / GET /api/lists: 95th percentile of response time is less than 500.0 : false "
             "(Could not find stats matching assertion path List(manager_review, GET /api/lists))"]
    for i in range(1, 101):  # 100 samples over 10 s: 1..100 ms, every 25th failed
        journey = "rep_daily" if i % 2 else "rep_landing"
        lines.append(f"LT_SAMPLE\t{1_000_000 + i * 100}\t{journey}\tGET /api/inbox\t{500 if i % 25 == 0 else 200}"
                     f"\t{0 if i % 25 == 0 else 1}\t{i}")
    lines.append("LT_SAMPLE\t1010100\trep_daily\tGET /api/accounts/:account_id\t0\t0\t")  # connection error
    assert tools.latest_gatling_report(tmp_path) == report
    m = tools.parse_gatling("\n".join(lines), report)
    assert m["requests"] == 101
    assert (m["p50_ms"], m["p95_ms"], m["p99_ms"], m["max_ms"]) == (50.0, 95.0, 99.0, 100.0)
    assert m["error_rate_pct"] == round(5 * 100 / 101, 2)
    assert m["rps"] == 10.1
    assert m["by_request"]["GET /api/accounts/:account_id"] == {
        "requests": 1, "p95_ms": None, "p99_ms": None, "max_ms": None, "error_rate_pct": 100.0}
    assert set(m["by_journey"]) == {"rep_daily", "rep_landing"}
    assert [a["passed"] for a in m["tool_assertions"]] == [False, False]
    assert tools.percentile([], 95) is None


def test_existing_personas_name_the_missing_variables():
    with pytest.raises(testdata.DataError, match="LT_PLATFORM_ADMIN_EMAIL"):
        testdata.prepare("existing", CAT, ["rep", "platform_admin"], "lt-1",
                         environ={"LT_REP_EMAIL": "a@qa.example.com", "LT_REP_PASSWORD": "x"})


def test_persona_files_inside_the_repo_are_refused():
    with pytest.raises(testdata.DataError, match="outside"):
        testdata.personas_from_env(CAT, ["rep"], {"LT_PERSONAS_FILE": str(REPO_ROOT / "personas.json")})


def test_synth_adapter_fails_closed_and_always_offers_cleanup():
    calls = []

    def fake(cmd, **_):
        calls.append(cmd)
        if "--cleanup" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"deleted": 1}\n', stderr="")
        prefix = cmd[cmd.index("--prefix") + 1]
        out = {"tenants": [f"{prefix}-0"],
               "personas": [{"role": "rep", "email": "r@qa.example.com", "password": "p", "tenant": f"{prefix}-0"}]}
        return subprocess.CompletedProcess(cmd, 0, stdout="seeding...\n" + json.dumps(out) + "\n", stderr="")

    ds = testdata.prepare("synth", CAT, ["rep"], "lt-20260915-k6-load-ab12", environ={"LT_SYNTH_COMMAND": "synth"},
                          runner=fake)
    assert ds.tenant_prefix == "qa-synth-lt-ab12" and ds.personas[0]["persona"] == "rep"
    assert ds.cleanup()["result"] == {"deleted": 1}

    def leaky(cmd, **_):
        out = {"tenants": ["acme-prod"], "personas": []}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    with pytest.raises(testdata.DataError, match="outside prefix"):
        testdata.prepare("synth", CAT, ["rep"], "lt-x-1", environ={"LT_SYNTH_COMMAND": "synth"}, runner=leaky)
    with pytest.raises(testdata.DataError, match="opt-in"):
        testdata.prepare("anon", CAT, ["rep"], "lt-x-1", environ={})


def test_persona_logins_respect_the_rate_limit_and_refuse_mfa_and_wrong_roles():
    responses = [preflight.Response(429, 5, headers={"Retry-After": "3"}),
                 preflight.Response(200, 5, body=b'{"access_token": "t", "role": "rep"}')]
    slept = []
    minted = preflight.login_personas("https://s", CAT, [{"persona": "rep", "email": "r@qa.example.com", "password": "p"}],
                                      {}, probe=lambda *a, **k: responses.pop(0), sleep=slept.append, clock=lambda: 1.0)
    assert minted[0]["token"] == "t" and slept == [3.0]

    def mfa(*_, **__):
        return preflight.Response(200, 5, body=b'{"challenge_token": "c"}')

    with pytest.raises(preflight.PreflightError, match="MFA"):
        preflight.login_personas("https://s", CAT, [{"persona": "rep", "email": "r@x", "password": "p"}], {}, probe=mfa)

    def admin(*_, **__):
        return preflight.Response(200, 5, body=b'{"access_token": "t", "role": "admin"}')

    with pytest.raises(preflight.PreflightError, match="expected 'rep'"):
        preflight.login_personas("https://s", CAT, [{"persona": "rep", "email": "r@x", "password": "p"}], {}, probe=admin)


def test_platform_access_explains_404_and_403():
    ok, why = preflight.platform_access("https://s", "t", {}, probe=lambda *a, **k: preflight.Response(404, 1))
    assert not ok and "not a platform admin" in why
    ok, why = preflight.platform_access(
        "https://s", "t", {}, probe=lambda *a, **k: preflight.Response(403, 1, body=b'{"detail": "not permitted"}'))
    assert not ok and "not permitted" in why


def test_b1ms_connection_budget():
    rows = {r["scenario"]: r for r in capacity.rows(capacity.STAGING)}
    assert rows["steady, 1 replica"]["ceiling"] == 30 and rows["steady, 1 replica"]["ceiling_verdict"] == "OK"
    assert rows["rollout at 1 replica (old+new)"]["ceiling_verdict"] == "EXCEEDS"
    assert rows["rollout while at app_max=2"]["expected_verdict"] == "EXCEEDS"
    assert capacity.headroom_replicas(capacity.STAGING) == 1
    bigger = capacity.Shape("x", "B_Standard_B2s", pool=5, overflow=5, app_max=3)
    assert all(r["ceiling_verdict"] == "OK" for r in capacity.rows(bigger))


def test_notice_failures_are_reported_not_fatal():
    printed = []
    info = {"run_id": "lt-1", "env": "staging", "tool": "k6", "profile": "load", "ends_at": "22:00 IST",
            "peak_vus": 20, "est_rps": 10}
    sinks = notify.notice("start", info, environ={"LT_NOTICE_WEBHOOK": "https://hook", "TF_BUILD": "True"},
                          printer=printed.append, post=lambda *a, **k: (False, "HTTP 500"))
    assert any("UNDER LOAD" in p for p in printed)
    assert any(p.startswith("##vso[task.logissue type=warning]") for p in printed)
    assert {"sink": "webhook", "ok": False, "detail": "HTTP 500"} in sinks
