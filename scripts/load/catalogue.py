"""The scenario catalogue: validate it, resolve load plans, generate what both tools execute.

k6 and Gatling never hold their own copy of a journey. This module turns
``quality/load/scenarios.yaml`` + ``environments.yaml`` into ``quality/load/generated/catalogue.json``;
the k6 interpreter (``deploy/loadtest/k6/lib/journey.js``) and the Gatling interpreter
(``deploy/loadtest/gatling/src/lib/journey.ts``) both execute that JSON. Divergence is therefore
only possible in three ways, and ``check`` fails on each:

* the generated JSON is stale against the YAML;
* a journey uses a feature one interpreter does not declare in ``SUPPORTED_FEATURES``;
* a journey id is written into a tool's code instead of being read from the catalogue.

    python scripts/load/catalogue.py generate
    python scripts/load/catalogue.py check
    python scripts/load/catalogue.py plan --profile load --env staging [--vus 30] [--budget lookalikes=1]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slo as slo_mod  # noqa: E402
from common import (  # noqa: E402
    CATALOGUE_JSON,
    ENVIRONMENTS_FILE,
    GATLING_DIR,
    K6_DIR,
    SCENARIOS_FILE,
    SLO_JSON,
    ConfigError,
    dump_json,
    k6_duration,
    load_yaml,
    number,
    rel,
)
from safety import CAP_KEYS, check_caps  # noqa: E402

SCHEMA = "nexus.load.catalogue/v1"
PLAN_SCHEMA = "nexus.load.plan/v1"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ROLES = ("rep", "manager", "admin", "owner", "platform_admin")
PROVIDERS = ("apify", "brave", "exa", "firecrawl", "groq", "reacher", "serper", "stripe")
PICKS = ("first", "random")
TOOL_KINDS = {"k6": ("iterations", "ramping_vus", "constant_vus"), "gatling": ("open", "closed")}
GUARD_KEYS = ("error_rate_pct", "p95_ms", "min_requests", "window_s", "canary_interval_s",
              "canary_window", "canary_fail_ratio")

# Planning estimates. Only used to check caps before a run; the tools enforce the real limits.
EST_REQUEST_S = 0.3
K6_GRACEFUL_STOP_S = 30
K6_SETUP_S = 60          # persona logins in setup(), spaced for the login rate limit
GATLING_DRAIN_S = 90     # open-model users already mid-journey when injection ends
PAID_MAX_DURATION_S = 300

INTERPRETERS = {
    "k6": K6_DIR / "lib" / "journey.js",
    "gatling": GATLING_DIR / "src" / "lib" / "journey.ts",
}


def entrypoint(tool: str, profile: str) -> Path:
    return K6_DIR / f"{profile}.js" if tool == "k6" else GATLING_DIR / "src" / f"{profile}.gatling.ts"


_VAR = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def route_name(method: str, path: str) -> str:
    """The request name both tools tag with: ``GET /api/accounts/:account_id``.

    One name per route template, never per id — metric labels in Grafana stay low-cardinality —
    and ``:var`` rather than ``{var}`` because braces inside a tag value break k6 threshold keys.
    SLO route keys (FastAPI's ``{var}`` form) map onto it with the same substitution.
    """
    return method + " " + _VAR.sub(r":\1", path)


class _Errors(list):
    def fail_if_any(self, what: str) -> None:
        if self:
            raise ConfigError(f"{what} is invalid:\n  - " + "\n  - ".join(self))


def _vars_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return _VAR.findall(value)
    if isinstance(value, dict):
        return [v for item in value.values() for v in _vars_in(item)]
    if isinstance(value, list):
        return [v for item in value for v in _vars_in(item)]
    return []


def _think(raw: Any, where: str, errors: _Errors, default: dict) -> dict:
    think = raw if raw is not None else default
    try:
        lo = number(think, "min", where, minimum=0)
        hi = number(think, "max", where, minimum=0)
    except ConfigError as exc:
        errors.append(str(exc))
        return dict(default)
    if hi < lo:
        errors.append(f"{where}: max ({hi}) is below min ({lo})")
    return {"min": lo, "max": hi}


# ------------------------------------------------------------------------------ journeys
def _request(req: Any, where: str, known: set[str], *, paid_allowed: bool, defaults: dict,
             errors: _Errors) -> dict | None:
    if not isinstance(req, dict):
        errors.append(f"{where}: expected a mapping")
        return None
    method, path = str(req.get("method", "")).upper(), req.get("path")
    if method not in METHODS:
        errors.append(f"{where}.method: {req.get('method')!r} is not one of {METHODS}")
    if not isinstance(path, str) or not path.startswith("/api/"):
        errors.append(f"{where}.path: must start with /api/, got {path!r}")
        return None
    paid = bool(req.get("paid", False))
    if not paid_allowed:
        if paid:
            errors.append(f"{where}: paid requests belong in `paid_journeys`, not `journeys`")
        if method != "GET":
            errors.append(
                f"{where}: default journeys are read-only; {method} {path} belongs in "
                "`paid_journeys` (with a budget) if it is needed at all"
            )
    query = req.get("query") or {}
    if not isinstance(query, dict):
        errors.append(f"{where}.query: expected a mapping")
        query = {}
    body = req.get("json")
    used = _vars_in(path) + _vars_in(query) + _vars_in(body)
    for var in used:
        if var not in known:
            errors.append(f"{where}: uses {{{var}}} before any earlier step extracts it")
    expect = req.get("expect_status", defaults.get("expect_status", [200]))
    if not isinstance(expect, list) or not all(isinstance(s, int) for s in expect):
        errors.append(f"{where}.expect_status: expected a list of status codes")
        expect = [200]
    out: dict[str, Any] = {
        "method": method,
        "path": path,
        "name": route_name(method, path),
        "query": {str(k): ("" if v is None else str(v).lower() if isinstance(v, bool) else str(v))
                  for k, v in query.items()},
        "expect_status": expect,
        "paid": paid,
    }
    if body is not None:
        out["json"] = body
    extract = req.get("extract")
    if extract is not None:
        if (not isinstance(extract, dict) or not _ID.match(str(extract.get("var", "")))
                or not isinstance(extract.get("json_field"), str)
                or extract.get("pick", "random") not in PICKS):
            errors.append(f"{where}.extract: expected {{var, json_field, pick: first|random}}")
        else:
            out["extract"] = {"var": extract["var"], "json_field": extract["json_field"],
                              "pick": extract.get("pick", "random")}
    return out


def step_features(step: dict) -> set[str]:
    if step.get("login"):
        return {"login"}
    feats = {f"method:{r['method']}" for r in step["requests"]}
    if len(step["requests"]) > 1:
        feats.add("parallel")
    if step.get("requires"):
        feats.add("requires")
    if step.get("think"):
        feats.add("think_time")
    for r in step["requests"]:
        if r["query"]:
            feats.add("query")
        if _VAR.search(r["path"]):
            feats.add("path_vars")
        if _vars_in(r["query"]):
            feats.add("query_vars")
        if "json" in r:
            feats.add("json_body")
        if "extract" in r:
            feats.add(f"extract:{r['extract']['pick']}")
        if r["paid"]:
            feats.add("paid")
    return feats


def _journey(jid: str, raw: Any, where: str, personas: dict, defaults: dict, *, paid: bool,
             errors: _Errors) -> dict | None:
    if not _ID.match(jid):
        errors.append(f"{where}: journey id {jid!r} must be snake_case")
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping")
        return None
    persona = raw.get("persona")
    if persona not in personas:
        errors.append(f"{where}.persona: {persona!r} is not a declared persona")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        errors.append(f"{where}.steps: expected a non-empty list")
        return None
    think = _think(raw.get("think_time_s"), f"{where}.think_time_s", errors,
                   defaults["think_time_s"])
    known: set[str] = set()
    seen: set[str] = set()
    steps = []
    for index, step in enumerate(steps_raw):
        sw = f"{where}.steps[{index}]"
        if not isinstance(step, dict) or not _ID.match(str(step.get("id", ""))):
            errors.append(f"{sw}: expected a mapping with a snake_case `id`")
            continue
        sid = step["id"]
        if sid in seen:
            errors.append(f"{sw}: duplicate step id {sid!r}")
        seen.add(sid)
        if step.get("login"):
            if index != 0:
                errors.append(f"{sw}: the login step must come first")
            steps.append({"id": sid, "login": True, "features": ["login"]})
            continue
        requires = step.get("requires") or []
        for var in requires:
            if var not in known:
                errors.append(f"{sw}.requires: {var!r} is not extracted by an earlier step")
        reqs = step.get("requests")
        if not isinstance(reqs, list) or not reqs:
            errors.append(f"{sw}.requests: expected a non-empty list")
            continue
        normalised = [
            r for i, req in enumerate(reqs)
            if (r := _request(req, f"{sw}.requests[{i}]", known, paid_allowed=paid,
                              defaults=defaults, errors=errors)) is not None
        ]
        out = {
            "id": sid,
            "login": False,
            "page": step.get("page"),
            "requires": list(requires),
            "think": bool(step.get("think", True)),
            "requests": normalised,
        }
        out["features"] = sorted(step_features(out))
        steps.append(out)
        # Extracted variables become available to LATER steps only: siblings in a parallel
        # batch are sent before any of them has answered.
        known.update(r["extract"]["var"] for r in normalised if "extract" in r)
    if steps and not steps[0].get("login"):
        errors.append(f"{where}: every journey starts with a `login: true` step")
    journey = {
        "id": jid,
        "description": " ".join(str(raw.get("description", "")).split()),
        "persona": persona,
        "think_time_s": think,
        "steps": steps,
        "features": sorted({f for s in steps for f in s["features"]}),
    }
    if paid:
        providers = raw.get("providers") or []
        bad = [p for p in providers if p not in PROVIDERS]
        if not providers or bad:
            errors.append(f"{where}.providers: expected a non-empty subset of {PROVIDERS}")
        try:
            journey["max_paid_calls"] = int(number(raw, "max_paid_calls", where, minimum=1,
                                                   integer=True))
        except ConfigError as exc:
            errors.append(str(exc))
            journey["max_paid_calls"] = 0
        if journey["max_paid_calls"] > 10:
            errors.append(f"{where}.max_paid_calls: a budget above 10 calls is not a smoke check")
        journey["providers"] = sorted(providers)
        journey["paid_requests_per_iteration"] = sum(
            1 for s in steps for r in s.get("requests", []) if r["paid"]
        )
        if journey["paid_requests_per_iteration"] == 0:
            errors.append(f"{where}: a paid journey must mark its paid request(s) `paid: true`")
    else:
        try:
            journey["weight"] = int(number(raw, "weight", where, minimum=0, integer=True))
        except ConfigError as exc:
            errors.append(str(exc))
            journey["weight"] = 0
    return journey


# ------------------------------------------------------------------------------ profiles
def _profile(name: str, raw: Any, where: str, errors: _Errors) -> dict | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping")
        return None
    tool, kind = raw.get("tool"), raw.get("kind")
    if tool not in TOOL_KINDS:
        errors.append(f"{where}.tool: expected one of {sorted(TOOL_KINDS)}")
        return None
    if kind not in TOOL_KINDS[tool]:
        errors.append(f"{where}.kind: {kind!r} is not a {tool} kind {TOOL_KINDS[tool]}")
        return None
    out: dict[str, Any] = {
        "name": name, "tool": tool, "kind": kind, "gate": bool(raw.get("gate", False)),
        "abort_is_result": bool(raw.get("abort_is_result", False)),
        "description": " ".join(str(raw.get("description", "")).split()),
        "think_time_scale": float(raw.get("think_time_scale", 1.0)),
    }
    try:
        if kind == "iterations":
            out["iterations"] = int(number(raw, "iterations", where, minimum=1, integer=True))
            out["max_duration_s"] = number(raw, "max_duration_s", where, minimum=10)
        elif kind == "ramping_vus":
            out["target_vus"] = int(number(raw, "target_vus", where, minimum=1, integer=True))
            out["stages"] = [
                {"duration_s": number(s, "duration_s", f"{where}.stages[{i}]", minimum=1),
                 "target": number(s, "target", f"{where}.stages[{i}]", minimum=0)}
                for i, s in enumerate(raw.get("stages") or [])
            ]
            if not out["stages"]:
                errors.append(f"{where}.stages: expected at least one stage")
        elif kind == "constant_vus":
            out["target_vus"] = int(number(raw, "target_vus", where, minimum=1, integer=True))
            out["duration_s"] = number(raw, "duration_s", where, minimum=10)
        elif kind == "open":
            if "stairs" in raw:
                out["stairs"] = _stairs(raw["stairs"], f"{where}.stairs")
            else:
                out["peak_users_per_sec"] = number(raw, "peak_users_per_sec", where, minimum=0.001)
                stages = []
                for i, s in enumerate(raw.get("stages") or []):
                    sw = f"{where}.stages[{i}]"
                    if ("ramp_to" in s) == ("hold" in s):
                        errors.append(f"{sw}: exactly one of `ramp_to` or `hold`")
                        continue
                    key = "ramp_to" if "ramp_to" in s else "hold"
                    stages.append({key: number(s, key, sw, minimum=0),
                                   "duration_s": number(s, "duration_s", sw, minimum=1)})
                if not stages:
                    errors.append(f"{where}.stages: expected at least one stage")
                out["stages"] = stages
        elif kind == "closed":
            out["stairs"] = _stairs(raw.get("stairs") or {}, f"{where}.stairs", integer=True)
    except ConfigError as exc:
        errors.append(str(exc))
        return None
    return out


def _stairs(raw: dict, where: str, *, integer: bool = False) -> dict:
    return {
        "start": number(raw, "start", where, minimum=0, integer=integer),
        "increment": number(raw, "increment", where, minimum=0.001, integer=integer),
        "levels": int(number(raw, "levels", where, minimum=1, integer=True)),
        "level_duration_s": number(raw, "level_duration_s", where, minimum=1),
        "ramp_s": number(raw, "ramp_s", where, minimum=0),
    }


# ------------------------------------------------------------------------------ loading
def load(scenarios: Any = None, environments: Any = None) -> dict:
    """Validated, normalised catalogue. Raises ConfigError listing EVERY problem found."""
    scen = load_yaml(SCENARIOS_FILE) if scenarios is None else scenarios
    envs = load_yaml(ENVIRONMENTS_FILE) if environments is None else environments
    errors = _Errors()
    if not isinstance(scen, dict) or scen.get("version") != 1:
        raise ConfigError("scenarios.yaml: expected `version: 1` at the top level")
    if not isinstance(envs, dict) or envs.get("version") != 1:
        raise ConfigError("environments.yaml: expected `version: 1` at the top level")

    auth = scen.get("auth") or {}
    for key in ("token_ttl_s", "refresh_after_s", "refresh_spread_s"):
        try:
            number(auth, key, "auth", minimum=1)
        except ConfigError as exc:
            errors.append(str(exc))
    if auth.get("refresh_after_s", 0) + auth.get("refresh_spread_s", 0) >= auth.get("token_ttl_s", 0):
        errors.append("auth: refresh_after_s + refresh_spread_s must end before token_ttl_s")
    rate = auth.get("login_rate_limit") or {}
    if not isinstance(rate.get("max"), int) or not isinstance(rate.get("window_s"), int):
        errors.append("auth.login_rate_limit: expected {max, window_s} integers")

    defaults = scen.get("defaults") or {}
    defaults = {
        "think_time_s": _think(defaults.get("think_time_s"), "defaults.think_time_s", errors,
                               {"min": 1, "max": 3}),
        "expect_status": defaults.get("expect_status", [200]),
    }

    personas = {}
    for pid, body in (scen.get("personas") or {}).items():
        if not isinstance(body, dict) or body.get("role") not in ROLES:
            errors.append(f"personas.{pid}.role: expected one of {ROLES}")
            continue
        if not re.fullmatch(r"LT_[A-Z][A-Z_]*", str(body.get("env_prefix", ""))):
            errors.append(f"personas.{pid}.env_prefix: expected LT_<NAME>")
            continue
        personas[pid] = {"id": pid, "role": body["role"], "env_prefix": body["env_prefix"],
                         "platform": bool(body.get("platform", False))}

    journeys = {
        jid: j for jid, raw in (scen.get("journeys") or {}).items()
        if (j := _journey(jid, raw, f"journeys.{jid}", personas, defaults, paid=False,
                          errors=errors)) is not None
    }
    if not journeys or not any(j["weight"] > 0 for j in journeys.values()):
        errors.append("journeys: at least one journey with weight > 0 is required")
    paid = {
        jid: j for jid, raw in (scen.get("paid_journeys") or {}).items()
        if (j := _journey(jid, raw, f"paid_journeys.{jid}", personas, defaults, paid=True,
                          errors=errors)) is not None
    }
    for jid in set(journeys) & set(paid):
        errors.append(f"{jid!r} is both a journey and a paid journey")
    forbidden = sorted(scen.get("forbidden_providers") or [])
    for jid, j in paid.items():
        clash = set(j["providers"]) & set(forbidden)
        if clash:
            errors.append(f"paid_journeys.{jid}: uses forbidden provider(s) {sorted(clash)}")

    profiles = {
        name: p for name, raw in (scen.get("profiles") or {}).items()
        if (p := _profile(name, raw, f"profiles.{name}", errors)) is not None
    }

    environments = {}
    for name, env in (envs.get("environments") or {}).items():
        ew = f"environments.{name}"
        caps, guards = env.get("caps") or {}, env.get("guards") or {}
        for key in CAP_KEYS:
            if key not in caps:
                errors.append(f"{ew}.caps: missing `{key}`")
        for key in GUARD_KEYS:
            if key not in guards:
                errors.append(f"{ew}.guards: missing `{key}`")
        if not env.get("allowed_hosts"):
            errors.append(f"{ew}.allowed_hosts: must list at least one host")
        environments[name] = {
            "name": name,
            "base_url": env.get("base_url"),
            "host_url": env.get("host_url", env.get("base_url")),
            "insecure_tls": bool(env.get("insecure_tls", False)),
            "docker_network": env.get("docker_network"),
            "allowed_hosts": list(env.get("allowed_hosts") or []),
            "window": env.get("window") or {"enforce": False},
            "caps": caps,
            "guards": guards,
            "worker_probe": env.get("worker_probe") or {"source": "none"},
        }
    if not envs.get("forbidden_hosts"):
        errors.append("environments.yaml: `forbidden_hosts` must name production")

    errors.fail_if_any("the load catalogue")
    catalogue = {
        "schema": SCHEMA,
        "sources": [rel(SCENARIOS_FILE), rel(ENVIRONMENTS_FILE)],
        "auth": auth,
        "run_header": scen.get("run_header", "X-Load-Test"),
        "defaults": defaults,
        "personas": personas,
        "journeys": journeys,
        "paid_journeys": paid,
        "forbidden_providers": forbidden,
        "probes": scen.get("probes") or {},
        "profiles": profiles,
        "timezone": envs.get("timezone", "UTC"),
        "forbidden_hosts": list(envs.get("forbidden_hosts") or []),
        "environments": environments,
    }
    # A default plan that no environment can run would only be discovered at 21:00 on staging.
    for pname in profiles:
        plan = build_plan(catalogue, pname)
        for ename, env in environments.items():
            for problem in check_caps(plan["envelope"], env["caps"]):
                errors.append(f"profiles.{pname} default plan exceeds {ename} caps: {problem}")
    errors.fail_if_any("the load catalogue")
    return catalogue


# ------------------------------------------------------------------------------ planning
def allocate(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Split ``total`` virtual users across journeys by weight.

    Every weighted journey gets at least one user once there are enough to go round (a 2% journey
    that never runs is a journey nobody tested), then the rest go by largest remainder. The sum is
    always exactly ``total``.
    """
    active = sorted(((j, w) for j, w in weights.items() if w > 0), key=lambda jw: (-jw[1], jw[0]))
    out = {j: 0 for j in weights}
    if total <= 0 or not active:
        return out
    if total < len(active):
        for j, _ in active[:total]:
            out[j] = 1
        return out
    for j, _ in active:
        out[j] = 1
    remaining = total - len(active)
    weight_sum = sum(w for _, w in active)
    shares = [(j, remaining * w / weight_sum) for j, w in active]
    for j, share in shares:
        out[j] += int(math.floor(share))
    left = remaining - sum(int(math.floor(s)) for _, s in shares)
    for j, share in sorted(shares, key=lambda js: (-(js[1] - math.floor(js[1])), js[0]))[:left]:
        out[j] += 1
    return out


def journey_estimate(journey: dict, scale: float) -> tuple[float, int]:
    """(seconds per pass, requests per pass) — for cap checks only."""
    think = (journey["think_time_s"]["min"] + journey["think_time_s"]["max"]) / 2 * scale
    seconds, requests = 0.0, 0
    for step in journey["steps"]:
        if step.get("login"):
            continue
        seconds += EST_REQUEST_S + (think if step["think"] else 0.0)
        requests += len(step["requests"])
    return max(seconds, 0.5), requests


def _r(value: float) -> float:
    return round(value, 4)


def build_plan(catalogue: dict, profile_name: str, *, overrides: dict | None = None,
               budget: dict[str, int] | None = None) -> dict:
    """Resolve a profile into per-journey executor specs both tools read verbatim."""
    if profile_name not in catalogue["profiles"]:
        raise ConfigError(f"unknown profile {profile_name!r} "
                          f"(have: {', '.join(sorted(catalogue['profiles']))})")
    profile = dict(catalogue["profiles"][profile_name])
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    allowed = {
        "iterations": ("iterations",),
        "ramping_vus": ("vus", "duration_scale"),
        "constant_vus": ("vus", "duration_s", "duration_scale"),
        "open": ("levels", "duration_scale") if "stairs" in profile
        else ("peak_users_per_sec", "duration_scale"),
        "closed": ("levels", "duration_scale"),
    }[profile["kind"]]
    unknown = set(overrides) - set(allowed)
    if unknown:
        raise ConfigError(f"profile {profile_name!r} ({profile['kind']}) accepts overrides "
                          f"{list(allowed)}, not {sorted(unknown)}")
    if "duration_scale" in overrides:
        # Shortens every stage/level proportionally: the same shape as a rehearsal, not a new shape.
        factor = float(overrides["duration_scale"])
        if not 0.01 <= factor <= 1.0:
            raise ConfigError(f"duration_scale must be within 0.01..1.0, got {factor}")

        def scaled(seconds: float, floor: float = 5.0) -> float:
            return max(floor, round(seconds * factor))

        if "stages" in profile:
            profile["stages"] = [{**s, "duration_s": scaled(s["duration_s"])} for s in profile["stages"]]
        if "duration_s" in profile:
            profile["duration_s"] = scaled(profile["duration_s"], 30.0)
        if "stairs" in profile:
            profile["stairs"] = {**profile["stairs"],
                                 "level_duration_s": scaled(profile["stairs"]["level_duration_s"]),
                                 "ramp_s": scaled(profile["stairs"]["ramp_s"], 0.0)}
    if "vus" in overrides:
        profile["target_vus"] = int(overrides["vus"])
    if "duration_s" in overrides:
        profile["duration_s"] = float(overrides["duration_s"])
    if "iterations" in overrides:
        profile["iterations"] = int(overrides["iterations"])
    if "peak_users_per_sec" in overrides:
        profile["peak_users_per_sec"] = float(overrides["peak_users_per_sec"])
    if "levels" in overrides:
        profile["stairs"] = {**profile["stairs"], "levels": int(overrides["levels"])}

    journeys = catalogue["journeys"]
    weights = {jid: j["weight"] for jid, j in journeys.items()}
    scale = profile["think_time_scale"]
    estimates = {jid: journey_estimate(j, scale) for jid, j in journeys.items()}
    per_journey: dict[str, dict] = {}
    peak_vus = 0.0
    est_rps = 0.0
    peak_users_per_sec = 0.0

    def rps_for(vus_by_journey: dict[str, float]) -> float:
        return sum(v * estimates[j][1] / estimates[j][0] for j, v in vus_by_journey.items())

    kind = profile["kind"]
    if kind == "iterations":
        duration = profile["max_duration_s"]
        for jid in journeys:
            if weights[jid] > 0:
                per_journey[jid] = {"executor": "per-vu-iterations", "vus": 1,
                                    "iterations": profile["iterations"],
                                    "maxDuration": k6_duration(duration)}
        peak_vus = len(per_journey)
        est_rps = rps_for({j: 1 for j in per_journey})
        duration_s = duration + K6_SETUP_S
    elif kind == "ramping_vus":
        stage_allocs = [allocate(int(round(profile["target_vus"] * s["target"])), weights)
                        for s in profile["stages"]]
        for jid in journeys:
            if any(a[jid] for a in stage_allocs):
                per_journey[jid] = {
                    "executor": "ramping-vus", "startVUs": 0,
                    "stages": [{"duration": k6_duration(s["duration_s"]), "target": a[jid]}
                               for s, a in zip(profile["stages"], stage_allocs)],
                    "gracefulRampDown": k6_duration(K6_GRACEFUL_STOP_S),
                    "gracefulStop": k6_duration(K6_GRACEFUL_STOP_S),
                }
        peak = max(stage_allocs, key=lambda a: sum(a.values()))
        peak_vus = sum(peak.values())
        est_rps = rps_for(peak)
        duration_s = sum(s["duration_s"] for s in profile["stages"]) + K6_GRACEFUL_STOP_S + K6_SETUP_S
    elif kind == "constant_vus":
        alloc = allocate(profile["target_vus"], weights)
        for jid, vus in alloc.items():
            if vus:
                per_journey[jid] = {"executor": "constant-vus", "vus": vus,
                                    "duration": k6_duration(profile["duration_s"]),
                                    "gracefulStop": k6_duration(K6_GRACEFUL_STOP_S)}
        peak_vus = sum(alloc.values())
        est_rps = rps_for(alloc)
        duration_s = profile["duration_s"] + K6_GRACEFUL_STOP_S + K6_SETUP_S
    elif kind == "open":
        total_w = sum(w for w in weights.values() if w > 0)
        shares = {j: w / total_w for j, w in weights.items() if w > 0}
        if "stairs" in profile:
            st = profile["stairs"]
            peak_rate = st["start"] + st["increment"] * (st["levels"] - 1)
            for jid, share in shares.items():
                per_journey[jid] = {"open": [{
                    "type": "stairs", "start": _r(st["start"] * share),
                    "increment": _r(st["increment"] * share), "levels": st["levels"],
                    "level_s": st["level_duration_s"], "ramp_s": st["ramp_s"],
                }]}
            run_s = st["levels"] * st["level_duration_s"] + (st["levels"] - 1) * st["ramp_s"]
        else:
            peak = profile["peak_users_per_sec"]
            peak_rate = peak * max(max(s.get("ramp_to", 0), s.get("hold", 0))
                                   for s in profile["stages"])
            for jid, share in shares.items():
                steps, prev = [], 0.0
                for s in profile["stages"]:
                    if "hold" in s:
                        rate = _r(s["hold"] * peak * share)
                        steps.append({"type": "constant", "rate": rate, "duration_s": s["duration_s"]}
                                     if rate > 0 else {"type": "nothing", "duration_s": s["duration_s"]})
                        prev = s["hold"]
                    else:
                        steps.append({"type": "ramp", "from": _r(prev * peak * share),
                                      "to": _r(s["ramp_to"] * peak * share),
                                      "duration_s": s["duration_s"]})
                        prev = s["ramp_to"]
                per_journey[jid] = {"open": steps}
            run_s = sum(s["duration_s"] for s in profile["stages"])
        peak_users_per_sec = peak_rate
        concurrency = {j: peak_rate * share * estimates[j][0] for j, share in shares.items()}
        peak_vus = math.ceil(sum(concurrency.values()) - 1e-9)
        est_rps = sum(peak_rate * share * estimates[j][1] for j, share in shares.items())
        duration_s = run_s + GATLING_DRAIN_S
    else:  # closed
        st = profile["stairs"]
        start, inc = allocate(int(st["start"]), weights), allocate(int(st["increment"]), weights)
        for jid in journeys:
            if start[jid] or inc[jid]:
                per_journey[jid] = {"closed": [{
                    "type": "stairs_concurrent", "start": start[jid], "increment": inc[jid],
                    "levels": st["levels"], "level_s": st["level_duration_s"], "ramp_s": st["ramp_s"],
                }]}
        top = {j: start[j] + inc[j] * (st["levels"] - 1) for j in journeys}
        peak_vus = sum(top.values())
        est_rps = rps_for(top)
        peak_users_per_sec = sum(v / estimates[j][0] for j, v in top.items())
        duration_s = (st["levels"] * st["level_duration_s"] + (st["levels"] - 1) * st["ramp_s"]
                      + GATLING_DRAIN_S)

    paid_plan: dict[str, dict] = {}
    for jid, calls in (budget or {}).items():
        pj = catalogue["paid_journeys"].get(jid)
        if pj is None:
            raise ConfigError(f"--budget {jid}: not a paid journey "
                              f"(have: {', '.join(sorted(catalogue['paid_journeys']))})")
        if not isinstance(calls, int) or calls < 1:
            raise ConfigError(f"--budget {jid}={calls}: expected a positive whole number of calls")
        if calls > pj["max_paid_calls"]:
            raise ConfigError(f"--budget {jid}={calls} exceeds its documented max_paid_calls "
                              f"({pj['max_paid_calls']}); raising it is a catalogue change")
        iterations = calls // pj["paid_requests_per_iteration"]
        if iterations < 1:
            raise ConfigError(f"--budget {jid}={calls} buys no iteration "
                              f"({pj['paid_requests_per_iteration']} paid calls each)")
        paid_plan[jid] = {
            "iterations": iterations,
            "max_paid_calls": iterations * pj["paid_requests_per_iteration"],
            "providers": pj["providers"],
            "k6": {"executor": "shared-iterations", "vus": 1, "iterations": iterations,
                   "maxDuration": k6_duration(PAID_MAX_DURATION_S)},
        }
        peak_vus += 1

    relogin = kind in ("constant_vus", "ramping_vus") and duration_s > catalogue["auth"]["refresh_after_s"]
    personas_used = sorted({catalogue["journeys"][j]["persona"] for j in per_journey}
                           | {catalogue["paid_journeys"][j]["persona"] for j in paid_plan})
    return {
        "schema": PLAN_SCHEMA,
        "profile": profile_name,
        "tool": profile["tool"],
        "kind": kind,
        "gate": profile["gate"],
        "abort_is_result": profile["abort_is_result"],
        "think_time_scale": scale,
        "overrides": overrides,
        "journeys": per_journey,
        "paid": paid_plan,
        "personas": personas_used,
        "relogin": relogin,
        "max_duration_s": math.ceil(duration_s),
        "envelope": {
            "peak_vus": math.ceil(peak_vus - 1e-9),
            "duration_s": math.ceil(duration_s),
            "est_peak_rps": round(est_rps, 1),
            "peak_users_per_sec": round(peak_users_per_sec, 3),
            "paid_calls_max": sum(p["max_paid_calls"] for p in paid_plan.values()),
        },
    }


# ------------------------------------------------------------------------------ generation
def generate(catalogue: dict | None = None) -> dict:
    cat = catalogue or load()
    return {**cat, "default_plans": {p: build_plan(cat, p) for p in cat["profiles"]}}


def interpreter_declarations(tool: str) -> tuple[set[str], set[str]]:
    """(SUPPORTED_FEATURES, SUPPORTED_KINDS) literals parsed out of an interpreter source."""
    text = INTERPRETERS[tool].read_text(encoding="utf-8")
    found = {}
    for const in ("SUPPORTED_FEATURES", "SUPPORTED_KINDS"):
        match = re.search(const + r"[^=]*=\s*\[([^\]]*)\]", text, re.S)
        if not match:
            raise ConfigError(f"{rel(INTERPRETERS[tool])}: no `{const} = [...]` declaration")
        found[const] = set(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))
    return found["SUPPORTED_FEATURES"], found["SUPPORTED_KINDS"]


def check(catalogue: dict | None = None) -> list[str]:
    """Every way k6 and Gatling could drift from the catalogue. Empty list means in sync."""
    problems: list[str] = []
    try:
        cat = catalogue or load()
    except ConfigError as exc:
        return [str(exc)]
    expected = dump_json(generate(cat))
    actual = CATALOGUE_JSON.read_text(encoding="utf-8") if CATALOGUE_JSON.exists() else ""
    if actual != expected:
        problems.append(f"{rel(CATALOGUE_JSON)} is stale: run `python scripts/load/catalogue.py generate`")
    slo_expected = dump_json(slo_mod.export())
    slo_actual = SLO_JSON.read_text(encoding="utf-8") if SLO_JSON.exists() else ""
    if slo_actual != slo_expected:
        problems.append(f"{rel(SLO_JSON)} is stale: run `python scripts/load/catalogue.py generate`")

    all_journeys = {**cat["journeys"], **cat["paid_journeys"]}
    for tool in TOOL_KINDS:
        try:
            features, kinds = interpreter_declarations(tool)
        except (ConfigError, OSError) as exc:
            problems.append(str(exc))
            continue
        used = {f for j in all_journeys.values() for f in j["features"]}
        missing = sorted(used - features)
        if missing:
            problems.append(f"{tool} interpreter lacks feature(s) the catalogue uses: {missing}")
        for pname, profile in cat["profiles"].items():
            if profile["tool"] == tool and profile["kind"] not in kinds:
                problems.append(f"{tool} interpreter lacks profile kind {profile['kind']!r} ({pname})")
            if profile["tool"] == tool and not entrypoint(tool, pname).exists():
                problems.append(f"profile {pname!r} has no entry point {rel(entrypoint(tool, pname))}")
        # A journey id spelled out in tool code is a scenario maintained twice by hand.
        sources = [INTERPRETERS[tool]] + [
            entrypoint(tool, p) for p, prof in cat["profiles"].items()
            if prof["tool"] == tool and entrypoint(tool, p).exists()
        ]
        for src in sources:
            text = src.read_text(encoding="utf-8")
            for jid in all_journeys:
                if re.search(r"[\"'`]" + re.escape(jid) + r"[\"'`]", text):
                    problems.append(f"{rel(src)} hard-codes journey {jid!r}; read it from the catalogue")
    return problems


def write_generated() -> list[Path]:
    CATALOGUE_JSON.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_JSON.write_text(dump_json(generate()), encoding="utf-8")
    SLO_JSON.write_text(dump_json(slo_mod.export()), encoding="utf-8")
    return [CATALOGUE_JSON, SLO_JSON]


def parse_budget(items: list[str] | None) -> dict[str, int]:
    budget: dict[str, int] = {}
    for item in items or []:
        name, _, value = item.partition("=")
        if not name or not value.isdigit():
            raise ConfigError(f"--budget {item!r}: expected <paid_journey>=<calls>")
        budget[name] = int(value)
    return budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NEXUS load-test scenario catalogue")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="rewrite quality/load/generated/*.json")
    sub.add_parser("check", help="fail if either tool has drifted from the catalogue")
    plan = sub.add_parser("plan", help="print the resolved plan and its cap envelope")
    plan.add_argument("--profile", required=True)
    plan.add_argument("--env")
    plan.add_argument("--vus", type=int)
    plan.add_argument("--duration-s", type=float)
    plan.add_argument("--iterations", type=int)
    plan.add_argument("--peak-rate", type=float)
    plan.add_argument("--levels", type=int)
    plan.add_argument("--budget", action="append")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "generate":
            for path in write_generated():
                print(f"wrote {rel(path)}")
            return 0
        if args.cmd == "check":
            problems = check()
            for p in problems:
                print(f"DRIFT: {p}", file=sys.stderr)
            if not problems:
                print("catalogue, generated JSON and both interpreters are in sync")
            return 1 if problems else 0
        cat = load()
        result = build_plan(cat, args.profile, overrides={
            "vus": args.vus, "duration_s": args.duration_s, "iterations": args.iterations,
            "peak_users_per_sec": args.peak_rate, "levels": args.levels,
        }, budget=parse_budget(args.budget))
        if args.env:
            result["cap_violations"] = check_caps(result["envelope"],
                                                  cat["environments"][args.env]["caps"])
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ConfigError as exc:
        print(f"catalogue error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
