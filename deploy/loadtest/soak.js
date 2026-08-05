// O-3 — k6 soak test: a modest, steady load held for a long duration to surface leaks (memory,
// connections, file handles) and slow degradation that a short load test never sees.
//
//   BASE_URL=http://localhost:8000 SOAK_DURATION=30m k6 run deploy/loadtest/soak.js
//
// Watch alongside the Grafana dashboard: RSS and DB connections should stay flat; P95 should not
// drift upward over the run. Default 15m for a quick local check; use 24h before a real launch.
import http from "k6/http";
import { check, sleep } from "k6";
import { setupTokens } from "./auth.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
  scenarios: {
    soak: {
      executor: "constant-vus",
      vus: Number(__ENV.SOAK_VUS || 30),
      duration: __ENV.SOAK_DURATION || "15m",
    },
  },
  thresholds: {
    // No upward drift: the same SLO must hold at the END of a long run, not just the start.
    http_req_duration: ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
  },
};

export function setup() {
  return setupTokens(BASE);
}

export default function (data) {
  const authHeaders = { headers: { Authorization: `Bearer ${data.token}` } };
  const responses = http.batch([
    ["GET", `${BASE}/api/accounts?limit=50`, null, authHeaders],
    ["GET", `${BASE}/api/signals?limit=50`, null, authHeaders],
    ["GET", `${BASE}/api/inbox`, null, authHeaders],
  ]);
  for (const r of responses) {
    check(r, { "read 2xx": (x) => x.status >= 200 && x.status < 300 });
  }
  sleep(2);
}
