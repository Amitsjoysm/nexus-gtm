import copy
import json

import pytest

import catalogue
from common import CATALOGUE_JSON, ConfigError, dump_json, load_yaml, ENVIRONMENTS_FILE, SCENARIOS_FILE


@pytest.fixture(scope="module")
def sources():
    return load_yaml(SCENARIOS_FILE), load_yaml(ENVIRONMENTS_FILE)


@pytest.fixture(scope="module")
def cat(sources):
    return catalogue.load(*sources)


def test_committed_catalogue_is_in_sync_with_yaml_and_both_interpreters(cat):
    assert catalogue.check(cat) == []


def test_generation_is_deterministic(cat):
    assert dump_json(catalogue.generate(cat)) == dump_json(catalogue.generate(cat))
    assert json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))["schema"] == catalogue.SCHEMA


def test_brief_journeys_are_covered(cat):
    rep = [s["id"] for s in cat["journeys"]["rep_daily"]["steps"]]
    assert rep == ["login", "app_shell", "inbox", "accounts_list", "account_detail", "signals", "contacts"]
    manager_paths = {r["path"] for s in cat["journeys"]["manager_review"]["steps"] for r in s.get("requests", [])}
    assert {"/api/analytics/overview", "/api/lists"} <= manager_paths
    admin_paths = {r["path"] for s in cat["journeys"]["admin_settings"]["steps"] for r in s.get("requests", [])}
    assert {"/api/workspace/members", "/api/billing/usage", "/api/workspace/automation"} <= admin_paths
    assert cat["journeys"]["platform_customers"]["persona"] == "platform_admin"
    assert cat["probes"]["worker_health"]["metrics"][0] == "nexus_queue_depth"


def test_default_journeys_never_call_paid_providers(cat):
    for journey in cat["journeys"].values():
        for step in journey["steps"]:
            for req in step.get("requests", []):
                assert req["method"] == "GET" and not req["paid"], (journey["id"], req["name"])


def test_request_names_use_colon_vars(cat):
    names = {r["name"] for j in cat["journeys"].values() for s in j["steps"] for r in s.get("requests", [])}
    assert "GET /api/accounts/:account_id" in names
    assert not any("{" in n for n in names)


def _mutated(sources, mutate):
    scen, envs = copy.deepcopy(sources[0]), copy.deepcopy(sources[1])
    mutate(scen, envs)
    return scen, envs


@pytest.mark.parametrize("mutate, message", [
    (lambda s, e: s["journeys"]["rep_daily"]["steps"][2]["requests"].append({"method": "POST", "path": "/api/inbox"}),
     "read-only"),
    (lambda s, e: s["journeys"]["rep_daily"]["steps"][2]["requests"].append({"method": "GET", "path": "/api/x", "paid": True}),
     "paid requests belong"),
    (lambda s, e: s["journeys"]["rep_daily"]["steps"][2]["requests"].append({"method": "GET", "path": "/api/x/{nope}"}),
     "before any earlier step extracts"),
    (lambda s, e: s["journeys"]["rep_daily"]["steps"].reverse(), "login step must come first"),
    (lambda s, e: s["paid_journeys"]["lookalikes"].__setitem__("providers", ["stripe"]), "forbidden provider"),
    (lambda s, e: s["paid_journeys"]["lookalikes"].__setitem__("max_paid_calls", 50), "not a smoke check"),
    (lambda s, e: s["journeys"]["rep_daily"].__setitem__("persona", "ghost"), "not a declared persona"),
    (lambda s, e: s["profiles"]["load"].__setitem__("target_vus", 500), "exceeds staging caps"),
    (lambda s, e: e.__setitem__("forbidden_hosts", []), "must name production"),
    (lambda s, e: e["environments"]["staging"]["caps"].pop("max_rps"), "missing `max_rps`"),
])
def test_invalid_catalogues_fail_loudly(sources, mutate, message):
    with pytest.raises(ConfigError, match=message):
        catalogue.load(*_mutated(sources, mutate))


def test_allocate_sums_exactly_and_gives_every_weighted_journey_a_user():
    weights = {"a": 55, "b": 20, "c": 15, "d": 8, "e": 2, "zero": 0}
    for total in range(0, 80):
        alloc = catalogue.allocate(total, weights)
        assert sum(alloc.values()) == total
        assert alloc["zero"] == 0
        if total >= 5:
            assert all(alloc[j] >= 1 for j in "abcde")
    assert catalogue.allocate(3, weights) == {"a": 1, "b": 1, "c": 1, "d": 0, "e": 0, "zero": 0}


def test_budget_rules(cat):
    plan = catalogue.build_plan(cat, "smoke", budget={"lookalikes": 2})
    assert plan["paid"]["lookalikes"]["max_paid_calls"] == 2
    assert plan["envelope"]["paid_calls_max"] == 2
    with pytest.raises(ConfigError, match="exceeds its documented max_paid_calls"):
        catalogue.build_plan(cat, "smoke", budget={"lookalikes": 3})
    with pytest.raises(ConfigError, match="not a paid journey"):
        catalogue.build_plan(cat, "smoke", budget={"rep_daily": 1})
    assert catalogue.build_plan(cat, "smoke")["paid"] == {}


def test_overrides_are_profile_specific(cat):
    with pytest.raises(ConfigError, match="accepts overrides"):
        catalogue.build_plan(cat, "stress", overrides={"vus": 10})
    short = catalogue.build_plan(cat, "breakpoint", overrides={"duration_scale": 0.2})
    full = catalogue.build_plan(cat, "breakpoint")
    assert short["envelope"]["duration_s"] < full["envelope"]["duration_s"]
    assert short["envelope"]["peak_users_per_sec"] == full["envelope"]["peak_users_per_sec"]


def test_soak_longer_than_refresh_point_relogs_in(cat):
    assert catalogue.build_plan(cat, "soak")["relogin"] is False  # 30 min default
    assert catalogue.build_plan(cat, "soak", overrides={"duration_s": 3000})["relogin"] is True


def test_gatling_defaults_finish_before_token_refresh(cat):
    for name, profile in cat["profiles"].items():
        if profile["tool"] == "gatling":
            assert catalogue.build_plan(cat, name)["max_duration_s"] < cat["auth"]["refresh_after_s"]


def test_drift_check_catches_hard_coded_journeys_and_missing_features(cat, tmp_path, monkeypatch):
    k6 = tmp_path / "journey.js"
    k6.write_text('export const SUPPORTED_FEATURES = ["login"];\nexport const SUPPORTED_KINDS = ["iterations"];\n'
                  'const special = "rep_daily";\n', encoding="utf-8")
    monkeypatch.setitem(catalogue.INTERPRETERS, "k6", k6)
    problems = "\n".join(catalogue.check(cat))
    assert "lacks feature(s)" in problems
    assert "hard-codes journey 'rep_daily'" in problems
    assert "lacks profile kind 'ramping_vus'" in problems
