// What a k6 script is allowed to know, and nothing it may invent.
//
// Journeys, weights and think times come from quality/load/generated/catalogue.json; the load
// shape from the resolved plan (LT_PLAN_JSON from scripts/load/run.py, or the catalogue's default
// plan for a direct run); thresholds from the SLO export (LT_SLO_JSON, or generated/slo.json).
// Target, caps and forbidden hosts are re-checked here as well as in the runner, because a script
// run by hand never passed through the runner.
const REPO = (__ENV.LT_REPO || "/repo").replace(/\/+$/, "");

export const CATALOGUE = JSON.parse(open(`${REPO}/quality/load/generated/catalogue.json`));
const SLO_TEXT = __ENV.LT_SLO_JSON || open(`${REPO}/quality/load/generated/slo.json`);
export const SLO = JSON.parse(SLO_TEXT);

export const ENV_NAME = __ENV.LT_ENV || "local";
export const ENV = CATALOGUE.environments[ENV_NAME];
if (!ENV) {
  throw new Error(`LT_ENV=${ENV_NAME} is not defined in quality/load/environments.yaml`);
}
export const BASE_URL = (__ENV.LT_BASE_URL || ENV.base_url).replace(/\/+$/, "");
export const RUN_ID = __ENV.LT_RUN_ID || `adhoc-k6-${Date.now()}`;

function hostOf(url) {
  const match = /^https?:\/\/([^/:?#]+)/i.exec(url);
  return match ? match[1].toLowerCase() : "";
}

export function assertTarget() {
  const host = hostOf(BASE_URL);
  for (const stem of CATALOGUE.forbidden_hosts) {
    if (host === stem.toLowerCase() || host.startsWith(stem.toLowerCase())) {
      throw new Error(`refusing ${host}: matches forbidden host ${stem} (production is never a target)`);
    }
  }
  if (!ENV.allowed_hosts.map((h) => h.toLowerCase()).includes(host)) {
    throw new Error(`refusing ${host}: not in environments.${ENV_NAME}.allowed_hosts`);
  }
}

const CAP_PAIRS = [
  ["peak_vus", "max_vus"],
  ["duration_s", "max_duration_s"],
  ["est_peak_rps", "max_rps"],
  ["peak_users_per_sec", "max_users_per_sec"],
];

export function assertCaps(plan) {
  for (const [measured, cap] of CAP_PAIRS) {
    if ((plan.envelope[measured] || 0) > ENV.caps[cap]) {
      throw new Error(
        `refusing plan ${plan.profile}: ${measured}=${plan.envelope[measured]} exceeds ` +
          `${ENV_NAME}.caps.${cap}=${ENV.caps[cap]}`,
      );
    }
  }
}

export function planFor(profile) {
  const plan = __ENV.LT_PLAN_JSON ? JSON.parse(__ENV.LT_PLAN_JSON) : CATALOGUE.default_plans[profile];
  if (!plan) throw new Error(`no plan for profile ${profile}`);
  if (plan.profile !== profile) {
    throw new Error(`LT_PLAN_JSON is for profile ${plan.profile}, this script runs ${profile}`);
  }
  if (plan.tool !== "k6") throw new Error(`profile ${profile} belongs to ${plan.tool}, not k6`);
  assertTarget();
  assertCaps(plan);
  return plan;
}

// "GET /api/accounts/{account_id}" (FastAPI template, as SLO route keys are written) ->
// "GET /api/accounts/:account_id" (the request name tag; braces would break k6 threshold keys).
export function routeTag(route) {
  return route.replace(/\{([a-z_][a-z0-9_]*)\}/g, ":$1");
}
