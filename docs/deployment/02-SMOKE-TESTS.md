# 02 — Smoke tests: is it actually working?

Run every time you deploy. Takes ~2 minutes. Ordered so each check builds on the last — the first
failure tells you where to look.

```bash
# The STABLE app hostname, not a revision hostname. terraform output is correct now, but read
# it straight from Azure if you are ever unsure which revision is live:
#   az containerapp show -n nexus-prod-app -g nexus-prod-rg --query properties.configuration.ingress.fqdn -o tsv
cd ~/nexus/deploy/cloud/azure && export URL="$(terraform output -raw app_default_url)"
export RG=nexus-prod-rg
```

---

## 1. The app answers at all

```bash
curl -s -o /dev/null -w "health: %{http_code}\n" "$URL/health"
```

**200** = the process is up and serving HTTP. Nothing more. A container that cannot reach the
database still returns 200 here.

## 2. The app is actually *ready* — the check that matters

```bash
curl -s -o /dev/null -w "ready: %{http_code}\n" "$URL/ready"
```

**200 proves four things at once**: migrations ran to head, `apply_rls.py` created the
least-privilege `nexus_app` role, the app can authenticate as that role, and it can query.

Allow **2–3 minutes after a deploy** — first boot runs `alembic upgrade head` from an empty
database. If it is still non-200 after five minutes, go to
[08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md#ready-never-returns-200).

## 3. The SPA is served, not just the API

```bash
curl -s "$URL/" | head -c 300
```

Expect HTML with a `<div id="root">` and a `/assets/...js` reference. If you get JSON or a 404, the
frontend build stage did not make it into the image — check that `az acr build` ran from the
repository root and that `frontend/package-lock.json` was present.

## 4. Database connectivity, from inside the container

```bash
az containerapp exec --name nexus-prod-app --resource-group $RG \
  --command "python -c \"
import asyncio,os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def m():
    e=create_async_engine(os.environ['NEXUS_DATABASE_URL'])
    async with e.connect() as c:
        print('user      :', (await c.execute(text('select current_user'))).scalar())
        print('tables    :', (await c.execute(text(\\\"select count(*) from information_schema.tables where table_schema='public'\\\"))).scalar())
asyncio.run(m())\""
```

Expect `user: nexus_app` and a table count around 60+.

> **`user` must be `nexus_app`, not `nexus`.** If the API connects as the owner, Postgres RLS is
> bypassed entirely and tenant isolation is gone — with **no error and no log line**. Cross-tenant
> reads would simply succeed. This is the single most important line in this document.

## 5. RLS is enforced, not merely present

```bash
az containerapp exec --name nexus-prod-app --resource-group $RG \
  --command "python -c \"
import asyncio,os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def m():
    e=create_async_engine(os.environ['NEXUS_DATABASE_URL'])
    async with e.connect() as c:
        r=(await c.execute(text('select rolsuper, rolbypassrls from pg_roles where rolname=current_user'))).first()
        print('rolsuper:', r[0], ' rolbypassrls:', r[1])
        n=(await c.execute(text(\\\"select count(*) from pg_policies where schemaname='public'\\\"))).scalar()
        print('policies:', n)
asyncio.run(m())\""
```

Both flags must be **False**, and policy count should be 25+. A role with `BYPASSRLS` ignores every
policy while everything looks configured.

## 6. Valkey is reachable — the queue

```bash
az containerapp exec --name nexus-prod-app --resource-group $RG \
  --command "python -c \"
import os, redis
r = redis.from_url(os.environ['NEXUS_REDIS_URL'])
print('ping:', r.ping()); print('url :', os.environ['NEXUS_REDIS_URL'])\""
```

Expect `ping: True`. A connection error means the ACA internal DNS name did not resolve — see
[08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md#app-cannot-reach-valkey).

## 7. The worker is consuming, not crash-looping

```bash
az containerapp logs show --name nexus-prod-worker --resource-group $RG --tail 40
```

You want the heartbeat ticking and no repeated tracebacks. The worker is the scheduler — if it is
down, automation, billing rollups and period closes all silently stop while the app looks fine.

```bash
az containerapp revision list --name nexus-prod-worker --resource-group $RG \
  --query "[].{rev:name,active:properties.active,replicas:properties.replicas,state:properties.runningState}" -o table
```

## 8. No restart loop anywhere

```bash
for a in app worker valkey; do
  echo "--- nexus-prod-$a"
  az containerapp revision list --name nexus-prod-$a --resource-group $RG \
    --query "[?properties.active].{rev:name,health:properties.healthState,running:properties.runningState}" -o table
done
```

All should show `Healthy` / `Running`. A climbing restart count is the signal the metric alert
watches — and it only notifies anyone if `alarm_email` was set.

## 9. Metrics are exported

```bash
curl -s "$URL/metrics" | head -20
```

Expect Prometheus text. Empty or 500 means instrumentation failed to load — non-fatal by design
(the app wraps it in try/except so a bad dependency cannot 500 every endpoint), but it means you
are blind to queue lag and error rates.

---

## One-shot: all of it

```bash
cd ~/nexus/deploy/cloud/azure && URL="$(terraform output -raw app_default_url)" && \
for p in /health /ready /metrics; do
  printf "%-10s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL$p")"
done && \
for a in app worker valkey; do
  printf "%-18s %s\n" "nexus-prod-$a" \
    "$(az containerapp show -n nexus-prod-$a -g nexus-prod-rg --query properties.runningStatus -o tsv)"
done
```

Healthy output:

```
/health    200
/ready     200
/metrics   200
nexus-prod-app     Running
nexus-prod-worker  Running
nexus-prod-valkey  Running
```

Anything else — [08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md).
