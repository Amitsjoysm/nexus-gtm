// What a Gatling simulation is allowed to know, and nothing it may invent.
//
// The catalogue and the default SLO export are bundled by esbuild at `gatling run` time, from the
// same generated files k6 reads. The runner overrides the plan and SLO per run through
// LT_PLAN_JSON / LT_SLO_JSON. Target host, caps and token lifetime are re-checked here because a
// simulation started by hand never passed through scripts/load/run.py.
import { getEnvironmentVariable } from "@gatling.io/core";

import bundledCatalogue from "../../../../../quality/load/generated/catalogue.json";
import bundledSlo from "../../../../../quality/load/generated/slo.json";

declare const console: { log(...args: unknown[]): void };
export const log = (...args: unknown[]): void => console.log(...args);

export interface Extract {
  var: string;
  json_field: string;
  pick: "first" | "random";
}

export interface Request {
  method: string;
  path: string;
  name: string;
  query: Record<string, string>;
  expect_status: number[];
  paid: boolean;
  json?: unknown;
  extract?: Extract;
}

export interface Step {
  id: string;
  login: boolean;
  think?: boolean;
  requires?: string[];
  requests?: Request[];
  features: string[];
}

export interface Journey {
  id: string;
  persona: string;
  think_time_s: { min: number; max: number };
  steps: Step[];
  max_paid_calls?: number;
}

export interface OpenStep {
  type: "nothing" | "constant" | "ramp" | "stairs";
  duration_s?: number;
  rate?: number;
  from?: number;
  to?: number;
  start?: number;
  increment?: number;
  levels?: number;
  level_s?: number;
  ramp_s?: number;
}

export interface ClosedStep {
  type: "stairs_concurrent";
  start: number;
  increment: number;
  levels: number;
  level_s: number;
  ramp_s: number;
}

export interface Plan {
  profile: string;
  tool: string;
  kind: string;
  abort_is_result: boolean;
  think_time_scale: number;
  journeys: Record<string, { open?: OpenStep[]; closed?: ClosedStep[] }>;
  paid: Record<string, { iterations: number; max_paid_calls: number }>;
  personas: string[];
  max_duration_s: number;
  envelope: Record<string, number>;
}

export interface Guards {
  error_rate_pct: number;
  p95_ms: number;
  min_requests: number;
  window_s: number;
}

export interface Environment {
  base_url: string;
  allowed_hosts: string[];
  caps: Record<string, number>;
  guards: Guards;
}

export interface Catalogue {
  auth: {
    login_path: string;
    refresh_after_s: number;
    login_rate_limit: { max: number; window_s: number };
  };
  run_header: string;
  personas: Record<string, { env_prefix: string }>;
  journeys: Record<string, Journey>;
  paid_journeys: Record<string, Journey>;
  default_plans: Record<string, Plan>;
  environments: Record<string, Environment>;
  forbidden_hosts: string[];
}

export interface Slo {
  profile: string;
  is_fixture: boolean;
  api: { p95_ms: number; p99_ms: number; error_rate_pct: number };
  routes: Record<string, { p95_ms?: number; p99_ms?: number; error_rate_pct?: number }>;
}

export interface Runtime {
  catalogue: Catalogue;
  plan: Plan;
  slo: Slo;
  envName: string;
  env: Environment;
  baseUrl: string;
  runId: string;
}

const CAP_PAIRS: [string, string][] = [
  ["peak_vus", "max_vus"],
  ["duration_s", "max_duration_s"],
  ["est_peak_rps", "max_rps"],
  ["peak_users_per_sec", "max_users_per_sec"],
];

function hostOf(url: string): string {
  const match = /^https?:\/\/([^/:?#]+)/i.exec(url);
  return match ? match[1].toLowerCase() : "";
}

export function loadRuntime(profile: string): Runtime {
  const catalogue = bundledCatalogue as unknown as Catalogue;
  const envName = getEnvironmentVariable("LT_ENV", "local");
  const env = catalogue.environments[envName];
  if (!env) throw new Error(`LT_ENV=${envName} is not defined in quality/load/environments.yaml`);

  const planText = getEnvironmentVariable("LT_PLAN_JSON");
  const plan: Plan = planText ? JSON.parse(planText) : catalogue.default_plans[profile];
  if (!plan || plan.profile !== profile) throw new Error(`no plan for profile ${profile}`);
  if (plan.tool !== "gatling") throw new Error(`profile ${profile} belongs to ${plan.tool}, not Gatling`);

  const baseUrl = (getEnvironmentVariable("LT_BASE_URL") || env.base_url).replace(/\/+$/, "");
  const host = hostOf(baseUrl);
  for (const stem of catalogue.forbidden_hosts) {
    if (host === stem.toLowerCase() || host.startsWith(stem.toLowerCase())) {
      throw new Error(`refusing ${host}: matches forbidden host ${stem} (production is never a target)`);
    }
  }
  if (!env.allowed_hosts.map((h) => h.toLowerCase()).includes(host)) {
    throw new Error(`refusing ${host}: not in environments.${envName}.allowed_hosts`);
  }
  for (const [measured, cap] of CAP_PAIRS) {
    if ((plan.envelope[measured] || 0) > env.caps[cap]) {
      throw new Error(`refusing ${profile}: ${measured}=${plan.envelope[measured]} exceeds ${envName}.caps.${cap}`);
    }
  }
  // Simulations do not re-login: every Gatling run must finish on the tokens it started with.
  if (plan.max_duration_s >= catalogue.auth.refresh_after_s) {
    throw new Error(`refusing ${profile}: ${plan.max_duration_s}s outlives the ${catalogue.auth.refresh_after_s}s token refresh point`);
  }

  const sloText = getEnvironmentVariable("LT_SLO_JSON");
  const slo: Slo = sloText ? JSON.parse(sloText) : (bundledSlo as unknown as Slo);
  const runId = getEnvironmentVariable("LT_RUN_ID", `adhoc-gatling-${Date.now()}`);
  return { catalogue, plan, slo, envName, env, baseUrl, runId };
}

// "GET /api/accounts/{account_id}" (SLO route key) -> "GET /api/accounts/:account_id" (request name).
export const routeTag = (route: string): string => route.replace(/\{([a-z_][a-z0-9_]*)\}/g, ":$1");
