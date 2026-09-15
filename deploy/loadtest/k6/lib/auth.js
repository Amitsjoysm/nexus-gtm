// Persona logins for k6, shaped by two facts about the API.
//
// 1. Signup cannot bootstrap a load test: /api/auth/signup returns 403 ("Email verification
//    required") since the verification flow landed. A script that signed up got no token and every
//    read 401ed — an "80% error rate" that was a broken harness. So personas LOG IN with credentials
//    supplied by the environment (never the repo): LT_<PERSONA>_EMAIL / LT_<PERSONA>_PASSWORD, or a
//    pool in LT_PERSONAS_JSON written by the runner from synthetic test data.
//
// 2. Login is rate limited to auth.login_rate_limit (10 per 60 s) per client IP, shared across
//    replicas. Every virtual user on one load generator is ONE IP. So setup() logs each credential
//    in once and hands the tokens to every VU; a VU logs in itself only when its token nears expiry
//    (1 h), at a per-VU offset spread over auth.refresh_spread_s so re-logins trickle, not burst.
//
// An MFA-enrolled persona fails fast: its login returns a challenge token that authorizes only
// /auth/mfa/verify, and using it as a bearer token would turn the run into a wall of 401s.
import http from "k6/http";
import { sleep } from "k6";
import { Counter } from "k6/metrics";
import { BASE_URL, CATALOGUE, RUN_ID } from "./config.js";

const logins = new Counter("lt_logins");

function credentialsFor(personaIds) {
  if (__ENV.LT_PERSONAS_JSON) {
    const pool = JSON.parse(__ENV.LT_PERSONAS_JSON);
    const out = {};
    for (const id of personaIds) {
      out[id] = pool.filter((p) => p.persona === id);
      if (!out[id].length) throw new Error(`LT_PERSONAS_JSON has no credential for persona ${id}`);
    }
    return out;
  }
  const out = {};
  for (const id of personaIds) {
    const prefix = CATALOGUE.personas[id].env_prefix;
    const email = __ENV[`${prefix}_EMAIL`];
    const password = __ENV[`${prefix}_PASSWORD`];
    if (!email || !password) {
      throw new Error(`persona ${id} needs ${prefix}_EMAIL and ${prefix}_PASSWORD in the environment`);
    }
    out[id] = [{ persona: id, email, password }];
  }
  return out;
}

export function login(persona, credential) {
  const auth = CATALOGUE.auth;
  const params = {
    headers: { "Content-Type": "application/json", [CATALOGUE.run_header]: RUN_ID },
    tags: { kind: "login", name: `POST ${auth.login_path}`, persona },
    responseCallback: http.expectedStatuses(200, 429),
  };
  const body = JSON.stringify({ email: credential.email, password: credential.password });
  for (let attempt = 1; attempt <= 4; attempt++) {
    const res = http.post(`${BASE_URL}${auth.login_path}`, body, params);
    if (res.status === 429) {
      logins.add(1, { persona, outcome: "throttled" });
      sleep(Math.min(Number(res.headers["Retry-After"] || auth.login_rate_limit.window_s), 90));
      continue;
    }
    if (res.status !== 200) {
      logins.add(1, { persona, outcome: "failed" });
      // The status only: the body can echo the submitted email, and never the password.
      throw new Error(`login failed for persona ${persona}: HTTP ${res.status}`);
    }
    const json = res.json();
    if (!json.access_token) {
      logins.add(1, { persona, outcome: "mfa" });
      throw new Error(`persona ${persona} is MFA-enrolled; load-test personas must not be`);
    }
    logins.add(1, { persona, outcome: "ok" });
    return { token: json.access_token, role: json.role, issued_ms: Date.now() };
  }
  throw new Error(`login for persona ${persona} stayed rate limited after 4 attempts`);
}

// setup(): one login per credential, or the runner's pre-minted tokens.
export function setupTokens(personaIds) {
  if (__ENV.LT_TOKENS_JSON) {
    const minted = JSON.parse(__ENV.LT_TOKENS_JSON);
    const out = {};
    for (const id of personaIds) {
      out[id] = minted.filter((t) => t.persona === id).map((t) => ({ token: t.token, role: t.role, issued_ms: t.issued_ms }));
      if (!out[id].length) throw new Error(`LT_TOKENS_JSON has no token for persona ${id}`);
    }
    return out;
  }
  const creds = credentialsFor(personaIds);
  const { max, window_s: windowS } = CATALOGUE.auth.login_rate_limit;
  const total = Object.values(creds).reduce((n, list) => n + list.length, 0);
  // Burst the first few, then space the rest so setup never trips the limiter it is trying to respect.
  const spacingS = total > max - 2 ? windowS / (max - 2) : 0;
  const out = {};
  let n = 0;
  for (const id of personaIds) {
    out[id] = creds[id].map((c) => {
      if (n++ >= max - 2 && spacingS) sleep(spacingS);
      return login(id, c);
    });
  }
  return out;
}

// Per-VU token cache. Lives in module scope, i.e. once per VU runtime.
export class VuTokens {
  constructor(setupTokensByPersona, relogin) {
    this.state = {};
    this.seed = setupTokensByPersona;
    this.relogin = relogin;
    this.creds = null;
    const auth = CATALOGUE.auth;
    const slotMs = Math.max(1000, (auth.login_rate_limit.window_s * 1000) / (auth.login_rate_limit.max * 0.8));
    const slots = Math.max(1, Math.floor((auth.refresh_spread_s * 1000) / slotMs));
    this.refreshAtMs = auth.refresh_after_s * 1000 + ((((__VU || 1) * 7919) % slots) * slotMs);
    this.ttlMs = auth.token_ttl_s * 1000;
  }

  _slot(persona) {
    const list = this.seed[persona];
    if (!list || !list.length) throw new Error(`no token for persona ${persona}`);
    const index = ((__VU || 1) - 1) % list.length;
    if (!this.state[persona]) this.state[persona] = { index, ...list[index] };
    return this.state[persona];
  }

  token(persona) {
    const slot = this._slot(persona);
    const age = Date.now() - slot.issued_ms;
    if (age >= this.refreshAtMs && (this.relogin || age >= this.ttlMs - 60000)) this._refresh(persona, slot);
    return slot.token;
  }

  invalidate(persona) {
    this._slot(persona).issued_ms = 0;
  }

  _refresh(persona, slot) {
    if (!this.creds) this.creds = credentialsFor(Object.keys(this.seed));
    const fresh = login(persona, this.creds[persona][slot.index % this.creds[persona].length]);
    Object.assign(slot, fresh);
  }
}
