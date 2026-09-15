// The Gatling interpreter for catalogue journeys. It knows HOW to run a step, never WHICH steps exist.
//
// scripts/load/catalogue.py check parses the two declarations below and fails when the catalogue
// uses something this file does not implement — the same contract deploy/loadtest/k6/lib/journey.js
// declares, so neither tool can quietly run a different journey than the other.
import {
  ChainBuilder,
  GlobalStore,
  Session,
  StringBody,
  doIf,
  exec,
  getEnvironmentVariable,
  jsonPath,
  pause,
  responseTimeInMillis,
  scenario,
} from "@gatling.io/core";
import { HttpRequestActionBuilder, http, status } from "@gatling.io/http";

import { Journey, Request, Runtime, Step, log } from "./config";

export const SUPPORTED_FEATURES = [
  "login",
  "method:GET",
  "method:POST",
  "parallel",
  "requires",
  "think_time",
  "query",
  "path_vars",
  "query_vars",
  "json_body",
  "extract:first",
  "extract:random",
  "paid",
];
export const SUPPORTED_KINDS = ["open", "closed"];

const VAR = /\{([a-z_][a-z0-9_]*)\}/g;
const BUCKET_S = 10;

const tokenKey = (persona: string, index: number): string => `lt:token:${persona}:${index}`;
const tokenCountKey = (persona: string): string => `lt:tokens:${persona}`;

function fill(template: string, session: Session, encode: boolean): string {
  return template.replace(VAR, (_m: string, name: string) => {
    const value = String(session.get<string>(name));
    return encode ? encodeURIComponent(value) : value;
  });
}

function fillJson(value: unknown, session: Session): unknown {
  if (typeof value === "string") return fill(value, session, false);
  if (Array.isArray(value)) return value.map((v) => fillJson(v, session));
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = fillJson(v, session);
    return out;
  }
  return value;
}

// Session values saved by findAll() arrive as a JVM list, not a JS array.
function toArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const list = value as { size?: () => number; get?: (i: number) => unknown } | null;
  if (list && typeof list.size === "function" && typeof list.get === "function") {
    const out: unknown[] = [];
    for (let i = 0; i < list.size(); i++) out.push(list.get(i));
    return out;
  }
  return [];
}

function bearer(persona: string, session: Session): string {
  const count = GlobalStore.getOrDefault<number>(tokenCountKey(persona), 0);
  if (!count) throw new Error(`no token for persona ${persona}`);
  return `Bearer ${GlobalStore.get<string>(tokenKey(persona, session.userId() % count))}`;
}

// ------------------------------------------------------------------------------ tokens
// Every virtual user on one load generator is ONE client IP, and login is limited to
// auth.login_rate_limit per IP. So a bootstrap user logs each credential in once, before any
// journey starts, and every user shares those tokens (andThen in simulation.ts).
interface Credential {
  persona: string;
  email: string;
  password: string;
}

function credentials(rt: Runtime): Credential[] {
  const pool = getEnvironmentVariable("LT_PERSONAS_JSON");
  const wanted = rt.plan.personas;
  if (pool) {
    const all = JSON.parse(pool) as Credential[];
    return all.filter((c) => wanted.includes(c.persona));
  }
  return wanted.map((persona) => {
    const prefix = rt.catalogue.personas[persona].env_prefix;
    const email = getEnvironmentVariable(`${prefix}_EMAIL`);
    const password = getEnvironmentVariable(`${prefix}_PASSWORD`);
    if (!email || !password) throw new Error(`persona ${persona} needs ${prefix}_EMAIL and ${prefix}_PASSWORD`);
    return { persona, email, password };
  });
}

export function bootstrapScenario(rt: Runtime) {
  const minted = getEnvironmentVariable("LT_TOKENS_JSON");
  if (minted) {
    for (const t of JSON.parse(minted) as { persona: string; token: string }[]) {
      const n = GlobalStore.getOrDefault<number>(tokenCountKey(t.persona), 0);
      GlobalStore.put(tokenKey(t.persona, n), t.token);
      GlobalStore.put(tokenCountKey(t.persona), n + 1);
    }
    return scenario("bootstrap").exec((session: Session) => session);
  }
  const { max, window_s: windowS } = rt.catalogue.auth.login_rate_limit;
  const creds = credentials(rt);
  const spacing = creds.length > max - 2 ? windowS / (max - 2) : 0;
  let chain: ChainBuilder = exec((session: Session) => session);
  creds.forEach((c, index) => {
    if (spacing && index >= max - 2) chain = chain.pause(spacing);
    chain = chain
      .exec(
        http(`POST ${rt.catalogue.auth.login_path}`)
          .post(rt.catalogue.auth.login_path)
          .body(StringBody(JSON.stringify({ email: c.email, password: c.password })))
          .asJson()
          .check(status().is(200), jsonPath("$.access_token").optional().saveAs("__token")),
      )
      .exec((session: Session) => {
        const token = session.get<string>("__token");
        if (!token) {
          // No access_token on a 200 means an MFA challenge; either way this persona cannot run.
          log(`LT_BOOTSTRAP_FAILED persona=${c.persona} (bad credentials or MFA-enrolled)`);
          return session.markAsFailed();
        }
        const n = GlobalStore.getOrDefault<number>(tokenCountKey(c.persona), 0);
        GlobalStore.put(tokenKey(c.persona, n), token);
        GlobalStore.put(tokenCountKey(c.persona), n + 1);
        return session.remove("__token");
      });
  });
  return scenario("bootstrap").exec(chain);
}

// ------------------------------------------------------------------------------ guard
// Same formula as scripts/load/safety.py tool_guard_breach: error rate, and p95 over the guard
// exactly when more than 5% of requests are slower than it — counted per 10 s bucket in the
// GlobalStore, so every virtual user contributes to one rolling window.
function record(total: number, failed: number, slow: number): void {
  const bucket = Math.floor(Date.now() / (BUCKET_S * 1000));
  GlobalStore.update<string>(`lt:guard:${bucket}`, (old) => {
    const [t, f, s] = (old || "0,0,0").split(",").map(Number);
    return `${t + total},${f + failed},${s + slow}`;
  });
}

export function guardBreach(rt: Runtime): string | null {
  const guards = rt.env.guards;
  const now = Math.floor(Date.now() / (BUCKET_S * 1000));
  const buckets = Math.max(1, Math.round(guards.window_s / BUCKET_S));
  let total = 0;
  let failed = 0;
  let slow = 0;
  for (let i = 1; i <= buckets; i++) {
    const value = GlobalStore.get<string>(`lt:guard:${now - i}`);
    if (!value) continue;
    const [t, f, s] = value.split(",").map(Number);
    total += t;
    failed += f;
    slow += s;
  }
  if (total < guards.min_requests) return null;
  if ((failed * 100) / total >= guards.error_rate_pct) {
    return `error rate ${((failed * 100) / total).toFixed(1)}% >= guard ${guards.error_rate_pct}%`;
  }
  if (slow / total > 0.05) return `p95 above guard ${guards.p95_ms} ms (${slow}/${total} slower)`;
  const rps = total / (buckets * BUCKET_S);
  if (rps > rt.env.caps.max_rps * 1.2) return `${rps.toFixed(1)} rps exceeds cap ${rt.env.caps.max_rps}`;
  return null;
}

let breachLogged = false;

function guardTripped(rt: Runtime): boolean {
  const breach = guardBreach(rt);
  if (breach && !breachLogged) {
    breachLogged = true;
    GlobalStore.put("lt:guard:breach", breach);
    // scripts/load/run.py reads this line to mark the run aborted-by-guard.
    log(`LT_GUARD_BREACH ${breach}`);
  }
  return breach !== null;
}

// ------------------------------------------------------------------------------ steps
function requestBuilder(persona: string, step: Step, req: Request, index: number): HttpRequestActionBuilder {
  const url = (session: Session) => fill(req.path, session, true);
  const named = http(req.name);
  let builder: HttpRequestActionBuilder;
  switch (req.method) {
    case "GET":
      builder = named.get(url);
      break;
    case "POST":
      builder = named.post(url);
      break;
    default:
      throw new Error(`method ${req.method} is not implemented by the Gatling interpreter`);
  }
  for (const [key, value] of Object.entries(req.query)) {
    builder = builder.queryParam(key, (session: Session) => fill(value, session, false));
  }
  builder = builder.header("Authorization", (session: Session) => bearer(persona, session));
  if (req.json !== undefined) {
    builder = builder.body(StringBody((session: Session) => JSON.stringify(fillJson(req.json, session)))).asJson();
  }
  const [firstStatus, ...otherStatuses] = req.expect_status;
  const checks = [
    status().in(firstStatus, ...otherStatuses),
    status().saveAs(`__st_${step.id}_${index}`),
    responseTimeInMillis().saveAs(`__rt_${step.id}_${index}`),
  ];
  if (req.extract) {
    checks.push(jsonPath(`$[*].${req.extract.json_field}`).findAll().optional().saveAs(`__ex_${req.extract.var}`));
  }
  return builder.check(...checks);
}

function afterStep(rt: Runtime, journey: Journey, step: Step, session: Session): Session {
  let next = session;
  let failed = 0;
  let slow = 0;
  (step.requests || []).forEach((req, index) => {
    const statusKey = `__st_${step.id}_${index}`;
    const timeKey = `__rt_${step.id}_${index}`;
    const code = session.get<number>(statusKey);
    const ms = session.get<number>(timeKey);
    const status = code === null || code === undefined ? 0 : Number(code);
    const ok = req.expect_status.includes(status);
    if (!ok) failed++;
    if (ms !== null && ms !== undefined && Number(ms) > rt.env.guards.p95_ms) slow++;
    // One line per request. scripts/load/tools.py computes the result file's percentiles from
    // these, so it never depends on the layout of Gatling's HTML report (which moves between
    // releases). Cleared after reading so a repeated step cannot report a previous iteration.
    log(`LT_SAMPLE\t${Date.now()}\t${journey.id}\t${req.name}\t${status}\t${ok ? 1 : 0}\t${ms ?? ""}`);
    next = next.remove(statusKey).remove(timeKey);
    if (req.extract) {
      const values = toArray(session.get<unknown>(`__ex_${req.extract.var}`)).filter(
        (v) => v !== null && v !== undefined && v !== "",
      );
      if (values.length) {
        const chosen = req.extract.pick === "first" ? values[0] : values[Math.floor(Math.random() * values.length)];
        next = next.set(req.extract.var, String(chosen));
      }
    }
  });
  record((step.requests || []).length, failed, slow);
  // Request KOs are already in Gatling's stats; clear the sticky session flag so one failed page
  // does not mark every later step of this user as failed too.
  return next.markAsSucceeded();
}

export interface Budget {
  key: string;
  max: number;
}

function stepChain(rt: Runtime, journey: Journey, step: Step, budget?: Budget): ChainBuilder {
  const requests = step.requests || [];
  const builders = requests.map((req, i) => requestBuilder(journey.persona, step, req, i));
  const [first, ...rest] = builders;
  // The SPA fires a page's requests together; Gatling's resources() sends siblings concurrently.
  let chain: ChainBuilder = exec(rest.length ? first.resources(...rest) : first)
    .exec((session: Session) => afterStep(rt, journey, step, session))
    .stopLoadGeneratorIf((session: Session) => `guard: ${GlobalStore.getOrDefault("lt:guard:breach", "")}`, () => guardTripped(rt));
  const scale = rt.plan.think_time_scale;
  if (step.think && scale > 0) {
    const t = journey.think_time_s;
    // Whole milliseconds: a bare number is seconds and must be integral on the JVM side
    // (Duration.ofSeconds(long)), which a random think time never is.
    chain = chain.pause(() => ({
      amount: Math.round((t.min + Math.random() * (t.max - t.min)) * scale * 1000),
      unit: "milliseconds" as const,
    }));
  }
  const paidHere = requests.filter((r) => r.paid).length;
  const requires = step.requires || [];
  return doIf((session: Session) => {
    if (!requires.every((name) => session.contains(name))) return false;
    if (!budget || !paidHere) return true;
    // Reserve the paid calls atomically; a step that would exceed the budget never sends.
    let allowed = false;
    GlobalStore.update<number>(budget.key, (used) => {
      const u = used || 0;
      allowed = u + paidHere <= budget.max;
      return allowed ? u + paidHere : u;
    });
    return allowed;
  }).then(chain);
}

export function journeyChain(rt: Runtime, journey: Journey, budget?: Budget): ChainBuilder {
  let chain: ChainBuilder = exec((session: Session) => session);
  for (const step of journey.steps) {
    if (step.login) continue; // tokens come from the bootstrap population
    chain = chain.exec(stepChain(rt, journey, step, budget));
  }
  return chain;
}
