// Assembles k6 options, setup and exec functions for one profile from catalogue + plan + SLO.
//
// Thresholds are built from the SLO export, never written here:
//   http_req_duration{kind:api}  p(95) / p(99)          <- slo.api.p95_ms / p99_ms
//   http_req_failed{kind:api}    rate                   <- slo.api.error_rate_pct
//   per journey: http_req_duration{journey:<id>} p(95), checks{journey:<id>}
//   per route:   slo.routes."<METHOD> <path>" overrides, on the request name tag
// plus GUARD thresholds from environments.<env>.guards with abortOnFail — the guard stops the run
// to protect staging; the SLO thresholds grade it.
import { CATALOGUE, ENV, ENV_NAME, RUN_ID, SLO, planFor, routeTag } from "./config.js";
import { VuTokens, setupTokens } from "./auth.js";
import { runJourney } from "./journey.js";

function thresholds(plan) {
  const api = SLO.api;
  const guards = ENV.guards;
  const abort = { abortOnFail: true, delayAbortEval: `${guards.window_s}s` };
  const t = {
    "http_req_duration{kind:api}": [
      `p(95)<${api.p95_ms}`,
      `p(99)<${api.p99_ms}`,
      { threshold: `p(95)<${guards.p95_ms}`, ...abort },
    ],
    "http_req_failed{kind:api}": [
      `rate<${api.error_rate_pct / 100}`,
      { threshold: `rate<${guards.error_rate_pct / 100}`, ...abort },
    ],
  };
  // Always-true thresholds on counts: k6 only exports submetrics that carry a threshold, and the
  // runner needs per-journey request counts and error rates to write a comparable result file.
  t["http_reqs{kind:api}"] = ["count>=0"];
  const names = new Set();
  for (const id of Object.keys(plan.journeys)) {
    t[`http_req_duration{journey:${id}}`] = [`p(95)<${api.p95_ms}`];
    t[`checks{journey:${id}}`] = [`rate>=${1 - api.error_rate_pct / 100}`];
    t[`http_reqs{journey:${id}}`] = ["count>=0"];
    t[`http_req_failed{journey:${id}}`] = ["rate<=1"];
    for (const step of CATALOGUE.journeys[id].steps) {
      for (const req of step.requests || []) names.add(req.name);
    }
  }
  // Per route as well, so a result file can say WHICH request is slow — the same breakdown
  // Gatling's report gives, which keeps the two tools' results comparable row for row.
  for (const name of names) {
    t[`http_req_duration{name:${name}}`] = ["max>=0"];
    t[`http_req_failed{name:${name}}`] = ["rate<=1"];
  }
  for (const [route, override] of Object.entries(SLO.routes || {})) {
    const name = routeTag(route);
    if (override.p95_ms !== undefined || override.p99_ms !== undefined) {
      t[`http_req_duration{name:${name}}`] = [
        ...(override.p95_ms !== undefined ? [`p(95)<${override.p95_ms}`] : []),
        ...(override.p99_ms !== undefined ? [`p(99)<${override.p99_ms}`] : []),
      ];
    }
    if (override.error_rate_pct !== undefined) {
      t[`http_req_failed{name:${name}}`] = [`rate<${override.error_rate_pct / 100}`];
    }
  }
  return t;
}

export function configure(profile) {
  const plan = planFor(profile);
  const scenarios = {};
  for (const [id, spec] of Object.entries(plan.journeys)) {
    scenarios[id] = { ...spec, exec: "journey", env: { LT_JOURNEY: id }, tags: { journey: id } };
  }
  for (const [id, paid] of Object.entries(plan.paid || {})) {
    scenarios[`paid_${id}`] = { ...paid.k6, exec: "paidJourney", env: { LT_JOURNEY: id }, tags: { journey: id, paid: "true" } };
  }

  const options = {
    scenarios,
    thresholds: thresholds(plan),
    // A hard ceiling k6 itself enforces by delaying requests, independent of the plan's estimate.
    rps: ENV.caps.max_rps,
    setupTimeout: "180s",
    summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)", "count"],
    userAgent: `nexus-loadtest/k6 ${RUN_ID}`,
    tags: { testid: RUN_ID, env: ENV_NAME, profile },
    // Every tag becomes a Prometheus label when metrics stream to Grafana Cloud. The default set
    // includes `url` (the real URL, account ids and all), `vu` and `iter` — unbounded cardinality.
    // `name` already carries the route template, so the raw URL adds nothing but series.
    systemTags: ["proto", "status", "method", "name", "group", "check", "error_code", "scenario", "expected_response"],
  };

  let tokens = null;
  const vuTokens = (data) => tokens || (tokens = new VuTokens(data.tokens, plan.relogin));
  const paidBudgets = {};

  return {
    options,
    setup() {
      return { tokens: setupTokens(plan.personas), started_ms: Date.now() };
    },
    journey(data) {
      runJourney(CATALOGUE.journeys[__ENV.LT_JOURNEY], vuTokens(data), {
        thinkTimeScale: plan.think_time_scale,
      });
    },
    paidJourney(data) {
      const id = __ENV.LT_JOURNEY;
      paidBudgets[id] = paidBudgets[id] || { used: 0, max: plan.paid[id].max_paid_calls };
      runJourney(CATALOGUE.paid_journeys[id], vuTokens(data), {
        thinkTimeScale: 0,
        paidBudget: paidBudgets[id],
      });
    },
  };
}
