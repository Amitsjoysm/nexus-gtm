// Shared auth for the k6 scripts.
//
// Signup is NOT usable as a load-test bootstrap any more: `/api/auth/signup` returns 403
// ("Email verification required. Use /auth/register/start to receive a code.") since the
// verification flow landed, so a script that signs up gets no token and every subsequent read
// 401s. That reads as an 80% error rate against the app when the truth is a broken harness —
// which is exactly what it did before this file existed.
//
// So: log in with real credentials supplied by env. The test is measuring the read-hot path,
// not the registration path, and a token is a token.
//
//   LOADTEST_EMAIL / LOADTEST_PASSWORD  — the rep whose daily path we replay.
//   LOADTEST_MGR_EMAIL / LOADTEST_MGR_PASSWORD  — optional. Analytics is manager+ (a `rep`
//   token gets 403 on /api/analytics/overview), so without these the analytics call is skipped
//   rather than counted as an error.
import http from "k6/http";

export function login(base, email, password) {
  const res = http.post(
    `${base}/api/auth/login`,
    JSON.stringify({ email, password }),
    { headers: { "Content-Type": "application/json" }, tags: { name: "login" } },
  );
  if (res.status !== 200) {
    throw new Error(`login failed for ${email}: ${res.status} ${res.body}`);
  }
  const body = res.json();
  if (!body.access_token) {
    // MFA returns a challenge token that authorizes only /auth/mfa/verify — useless here, and
    // silently treating it as a bearer token would produce a run of 401s instead of a clear stop.
    throw new Error(`no access_token for ${email} (MFA-enrolled account?)`);
  }
  return { token: body.access_token, role: body.role };
}

// Roles allowed to read /api/analytics/overview (manager and up).
const ANALYTICS_ROLES = ["owner", "admin", "manager"];

export function setupTokens(base) {
  const rep = login(
    base,
    __ENV.LOADTEST_EMAIL || "sdr@marketjoy.com",
    __ENV.LOADTEST_PASSWORD || "",
  );
  let analytics = ANALYTICS_ROLES.includes(rep.role) ? rep.token : null;
  if (!analytics && __ENV.LOADTEST_MGR_EMAIL) {
    analytics = login(base, __ENV.LOADTEST_MGR_EMAIL, __ENV.LOADTEST_MGR_PASSWORD || "").token;
  }
  return { token: rep.token, role: rep.role, analyticsToken: analytics };
}
