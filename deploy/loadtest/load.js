// O-3 — k6 load test: ramp virtual users and hammer the read-hot endpoints a rep hits all day.
// Produces P50/P90/P95/P99, throughput, and error rate; fails the run if the SLO thresholds break.
//
//   BASE_URL=http://localhost:8000 k6 run deploy/loadtest/load.js
//   (or via Docker: docker run --rm -e BASE_URL=http://host.docker.internal:8000 \
//                     -v "$PWD/deploy/loadtest:/s" grafana/k6 run /s/load.js)
import http from "k6/http";
import { check, sleep } from "k6";
import { Rate } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const errors = new Rate("business_errors");

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 50 },   // warm up
        { duration: "1m", target: 200 },    // sustained load
        { duration: "30s", target: 0 },     // ramp down
      ],
    },
  },
  thresholds: {
    // SLOs from the remediation plan (reads only; agent endpoints are intentionally slower).
    http_req_duration: ["p(95)<500", "p(99)<1500"],
    http_req_failed: ["rate<0.01"],
    business_errors: ["rate<0.01"],
  },
};

// One workspace + token for the whole test (created once).
export function setup() {
  const slug = `load-${Date.now()}`;
  const res = http.post(
    `${BASE}/api/auth/signup`,
    JSON.stringify({
      company_name: "Load Co", company_slug: slug, full_name: "Load Rep",
      email: `rep@${slug}.example`, password: "password123",
    }),
    { headers: { "Content-Type": "application/json" } },
  );
  check(res, { "signup 201": (r) => r.status === 201 });
  return { token: res.json("access_token") };
}

export default function (data) {
  const authHeaders = { headers: { Authorization: `Bearer ${data.token}` } };

  // The rep's daily read path.
  const health = http.get(`${BASE}/health`);
  check(health, { "health 200": (r) => r.status === 200 }) || errors.add(1);

  const responses = http.batch([
    ["GET", `${BASE}/api/accounts?limit=50`, null, authHeaders],
    ["GET", `${BASE}/api/signals?limit=50`, null, authHeaders],
    ["GET", `${BASE}/api/inbox`, null, authHeaders],
    ["GET", `${BASE}/api/analytics/overview`, null, authHeaders],
  ]);
  for (const r of responses) {
    check(r, { "read 2xx": (x) => x.status >= 200 && x.status < 300 }) || errors.add(1);
  }

  sleep(1); // model a rep pausing between actions
}
