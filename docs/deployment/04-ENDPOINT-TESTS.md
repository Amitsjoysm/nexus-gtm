# 04 — Endpoint liveness tests

Exercises the real API surface with a real token. Run after [02-SMOKE-TESTS.md](02-SMOKE-TESTS.md)
passes and after [03-SEED-USERS.md](03-SEED-USERS.md) has created a workspace.

## Setup

```bash
cd ~/nexus/deploy/cloud/azure && export URL="$(terraform output -raw app_default_url)"
export EMAIL="you@example.com" PASSWORD="your-password"

export TOKEN="$(curl -s -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"

[ -n "$TOKEN" ] && echo "token acquired (${#TOKEN} chars)" || echo "LOGIN FAILED"
```

If login returns a challenge instead of a token, MFA is enabled on that user — complete
`/auth/mfa/verify` first. A challenge token authorizes **nothing** except that endpoint, by design.

## Run

```bash
check() {  # check <METHOD> <PATH> <EXPECTED>
  code=$(curl -s -o /tmp/r.json -w '%{http_code}' -X "$1" "$URL$2" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --max-time 20)
  [ "$code" = "$3" ] && printf "  OK   %-6s %-34s %s\n" "$1" "$2" "$code" \
                     || printf "  FAIL %-6s %-34s %s (want %s)\n" "$1" "$2" "$code" "$3"
}

echo "── unauthenticated ──"
check GET  /health                 200
check GET  /ready                  200
check GET  /metrics                200

echo "── identity ──"
check GET  /api/auth/me                200
check GET  /api/auth/mfa               200

echo "── core product ──"
check GET  /api/accounts               200
check GET  /api/contacts               200
check GET  /api/inbox                  200
check GET  /api/signals                200
check GET  /api/lists                  200
check GET  /api/alerts                 200

echo "── workspace ──"
check GET  /api/settings               200
check GET  /api/team                   200
check GET  /api/billing/entitlements   200

echo "── network graph ──"
check GET  /api/network/summary        200
```

Paths vary by version — get the authoritative list from the running app:

```bash
curl -s "$URL/openapi.json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p, ops in sorted(d['paths'].items()):
    print(' '.join(m.upper() for m in ops), p)
" | head -60
```

## Interpreting failures

| Code | Meaning | Action |
|---|---|---|
| **401** | Token missing/expired | Re-run the login block |
| **403** | RBAC or platform-admin gate | Expected for `/admin/*` unless in `NEXUS_PLATFORM_ADMIN_EMAILS` |
| **402** | Billing entitlement blocked | Only when `NEXUS_BILLING_ENFORCEMENT=on`; default `shadow` never blocks |
| **404** | Path does not exist in this version | Check `openapi.json` |
| **429** | Auth rate limit | `auth_rate_limit_enabled` is on by default — wait 60s |
| **500** | Real bug | Pull logs (below) |
| **000** | Connection failed | App is down or the URL is wrong |

```bash
az containerapp logs show --name nexus-prod-app --resource-group nexus-prod-rg --tail 100 \
  | grep -iE "error|traceback|exception"
```

## Empty results are correct

`/accounts`, `/inbox`, `/signals` returning `[]` on a new workspace is **success**, not failure —
there is no data yet. `200` with an empty list is the check; a non-200 is the problem.

## Verify tenant isolation (do this once, seriously)

The whole security model rests on RLS. Create a second workspace and confirm it cannot see the
first one's data.

```bash
curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name":"Isolation Test","company_slug":"isolation-test",
  "email":"isolation@example.com","full_name":"Test","password":"isolation-test-pw-123"
}' >/dev/null

T2="$(curl -s -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"isolation@example.com","password":"isolation-test-pw-123"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

echo "tenant 1 accounts: $(curl -s "$URL/api/accounts" -H "Authorization: Bearer $TOKEN" | head -c 200)"
echo "tenant 2 accounts: $(curl -s "$URL/api/accounts" -H "Authorization: Bearer $T2"    | head -c 200)"
```

Tenant 2 must see **none** of tenant 1's accounts. Delete the test workspace afterwards.

> Under RLS a cross-tenant read returns **zero rows, not an error**. That is the dangerous property:
> a broken boundary looks like an empty account list, not like a failure. Which is why this is
> tested by creating real data in one workspace and confirming its absence in another, rather than
> by trusting that policies exist.

## Post-deploy gate

Minimum bar before calling a release good — this is what
[azure-pipelines-cd.yml](../../azure-pipelines-cd.yml) automates:

```bash
for p in /health /ready; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --retry 6 --retry-delay 5 \
    --retry-all-errors "$URL$p")
  [ "$code" = "200" ] || { echo "SMOKE FAILED on $p ($code)"; exit 1; }
done
echo "release is live and healthy"
```
