// The k6 interpreter for catalogue journeys. It knows HOW to run a step, never WHICH steps exist.
//
// scripts/load/catalogue.py check parses the two declarations below and fails when the catalogue
// uses something this file does not implement, so a new step feature cannot land in one tool only.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";
import { BASE_URL, CATALOGUE, RUN_ID } from "./config.js";

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
export const SUPPORTED_KINDS = ["iterations", "ramping_vus", "constant_vus"];

const skipped = new Counter("lt_skipped_steps");
const paidCalls = new Counter("lt_paid_calls");
const journeyDuration = new Trend("lt_journey_duration", true);
const reauth = new Counter("lt_reauth_retries");

const VAR = /\{([a-z_][a-z0-9_]*)\}/g;

function fill(template, vars, encode) {
  return String(template).replace(VAR, (_, name) => (encode ? encodeURIComponent(vars[name]) : vars[name]));
}

function fillJson(value, vars) {
  if (typeof value === "string") return fill(value, vars, false);
  if (Array.isArray(value)) return value.map((v) => fillJson(v, vars));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = fillJson(v, vars);
    return out;
  }
  return value;
}

function urlFor(req, vars) {
  const query = Object.entries(req.query)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(fill(v, vars, false))}`)
    .join("&");
  return `${BASE_URL}${fill(req.path, vars, true)}${query ? `?${query}` : ""}`;
}

function paramsFor(req, token, tags) {
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
    [CATALOGUE.run_header]: RUN_ID,
  };
  if (req.json !== undefined) headers["Content-Type"] = "application/json";
  return {
    headers,
    tags: { ...tags, name: req.name, kind: req.paid ? "paid" : "api" },
    responseCallback: http.expectedStatuses(...req.expect_status),
  };
}

function extractInto(vars, req, res) {
  if (!req.extract || !req.expect_status.includes(res.status)) return;
  let body;
  try {
    body = res.json();
  } catch (_) {
    return;
  }
  const values = (Array.isArray(body) ? body : [])
    .map((row) => row && row[req.extract.json_field])
    .filter((v) => v !== undefined && v !== null && v !== "");
  if (!values.length) return;
  vars[req.extract.var] =
    req.extract.pick === "first" ? values[0] : values[Math.floor(Math.random() * values.length)];
}

function send(req, vars, token, tags) {
  const body = req.json === undefined ? null : JSON.stringify(fillJson(req.json, vars));
  return [req.method, urlFor(req, vars), body, paramsFor(req, token, tags)];
}

export function runJourney(journey, tokens, options) {
  const vars = {};
  const persona = journey.persona;
  const scale = options.thinkTimeScale;
  const started = Date.now();
  for (const step of journey.steps) {
    const tags = { journey: journey.id, step: step.id, persona };
    if (step.login) {
      tokens.token(persona); // ensures a valid token; logs in only when due
      continue;
    }
    if (step.requires.some((name) => vars[name] === undefined)) {
      // Missing upstream data (e.g. a tenant with no accounts) is a data problem, not an API error.
      skipped.add(1, tags);
      continue;
    }
    if (options.paidBudget && step.requests.some((r) => r.paid)) {
      const paidHere = step.requests.filter((r) => r.paid).length;
      if (options.paidBudget.used + paidHere > options.paidBudget.max) {
        skipped.add(1, { ...tags, reason: "budget" });
        continue;
      }
      options.paidBudget.used += paidHere;
    }
    const batch = step.requests.map((req) => send(req, vars, tokens.token(persona), tags));
    let responses = batch.length === 1 ? [http.request(...batch[0])] : http.batch(batch);
    if (responses.some((r) => r.status === 401) && !step.requests.some((r) => r.paid)) {
      // A token revoked or expired early. Re-login once and replay the step; the 401 itself stays
      // counted, so a real auth regression is still visible. Paid requests are never replayed.
      reauth.add(1, tags);
      tokens.invalidate(persona);
      const retry = step.requests.map((req) => send(req, vars, tokens.token(persona), { ...tags, retry: "true" }));
      responses = retry.length === 1 ? [http.request(...retry[0])] : http.batch(retry);
    }
    responses.forEach((res, i) => {
      const req = step.requests[i];
      check(res, { [`${req.name} status`]: (r) => req.expect_status.includes(r.status) }, tags);
      if (req.paid) paidCalls.add(1, tags);
      extractInto(vars, req, res);
    });
    if (step.think && scale > 0) {
      const t = journey.think_time_s;
      sleep((t.min + Math.random() * (t.max - t.min)) * scale);
    }
  }
  journeyDuration.add(Date.now() - started, { journey: journey.id });
}
