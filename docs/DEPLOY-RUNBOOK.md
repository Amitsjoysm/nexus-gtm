# Deployment Runbook

Current as of 2026-08-26. Migration head: `0050_runtime_settings`.

This is the order things must happen in, and what to check after each. Steps 1–5 are the deploy;
steps 6–8 are the operator tasks that no amount of code can do for you.

---

## 1. Deploy the build

```bash
cd frontend && npm run build && cd ..
docker compose -f deploy/docker-compose.prod.yml build app worker
docker compose -f deploy/docker-compose.prod.yml up -d
```

The frontend build writes into `nexus/web/dist/`, which FastAPI serves with SPA fallback — so it
must run **before** the image build, not after.

## 2. Migrate

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app sh -c "cd /app && alembic upgrade head"
docker compose -f deploy/docker-compose.prod.yml exec -T app sh -c "cd /app && alembic current"
```

Expect `0050_runtime_settings (head)`. Then apply row-level security to any new tenant-scoped
tables:

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/apply_rls.py
```

`runtime_settings`, `payment_credentials`, `provider_keys` and `provider_settings` carry **no**
`tenant_id` and are deliberately not enrolled — they are deployment configuration, and enrolling
them would make the platform role see zero rows.

## 3. Move the plan ladder (existing deployments only)

A fresh database seeds the current ladder automatically. An existing one needs the superseded tiers
retired:

```bash
# Dry run FIRST. It prints subscriber counts per plan and writes nothing.
docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/restructure_plans.py
docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/restructure_plans.py --apply
```

**Read the dry run before applying.** Any plan showing subscribers is one you are withdrawing from
sale while people are on it — which is exactly what retiring is for, but it should be a decision
rather than a surprise. Nothing is deleted: `billing_subscriptions.plan_id` is a foreign key and
entitlements resolve from the plan row, so a delete would either violate the constraint or leave a
paying customer with no entitlements at all.

Verify:

```bash
curl -s localhost:8080/api/billing/plans -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

Expect exactly Free, Launch, Launch (annual), Accelerate, Accelerate (annual).

## 4. Grant the first platform admin

The Control plane is invisible without one. Either set `NEXUS_PLATFORM_ADMIN_EMAILS` in the
environment (the bootstrap path, deliberately carrying full power so a lockout cannot happen), or
insert a row:

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
import asyncio
from sqlalchemy import select
from nexus.core.db import get_platform_sessionmaker
from nexus.models.billing import PlatformAdmin
from nexus.billing.permissions import permissions_for_role
async def m():
    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(select(PlatformAdmin).where(
            PlatformAdmin.email=='you@example.com'))).first()
        if row is None:
            row = PlatformAdmin(email='you@example.com'); s.add(row)
        row.platform_role='superadmin'
        row.permissions=list(permissions_for_role('superadmin'))
        row.active=True
        await s.commit()
    print('granted')
asyncio.run(m())"
```

## 5. Smoke test

```bash
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/health          # 200
curl -s localhost:8080/api/billing/plans -H "Authorization: Bearer $TOKEN"
curl -s localhost:8080/api/admin/runtime/settings -H "Authorization: Bearer $TOKEN"
```

---

## 6. Stripe — the part that cannot be automated

**The webhook URL goes in the Stripe dashboard and nothing in this application can put it there.**

Control plane → **Configuration** → Stripe webhook shows the exact string to paste and the seven
events to select. Select only those: Stripe sends everything otherwise, and each unhandled type is
a delivery it records as failed, which looks like a fault in your dashboard.

Then press **Test connection**. It posts a correctly signed event at your own endpoint and confirms
the signing secret verifies and the route is live. It deliberately does **not** claim Stripe can
reach the host — that depends on DNS and firewalls outside this process. A green result here *plus*
a delivery visible in the Stripe dashboard is the complete picture.

Add the credentials under **Payments**. Verification is mandatory before activation, and it reads
`/v1/account` rather than merely authenticating: a key that works against the *wrong business* looks
exactly like success, and test and live keys are the same shape.

> **Until the endpoint is publicly reachable, subscription state changes only arrive via
> reconciliation.** Everything on our side works; Stripe simply has nowhere to deliver to.

## 7. Provider keys

Control plane → **Provider keys**. Add each credential and press **Test**. A green "In use" badge
marks the key actually serving that provider — computed from the same ordering the resolver uses,
so it cannot disagree with what is being spent.

`probe_ok` ("Auth OK") is amber on purpose and is **not** a tick: it means the credential
authenticates, not that the product works with it. On 2026-08-21 five Groq keys passed auth and
404'd on every completion because the configured model had been withdrawn, and the stub wrote every
outbound email. Press **Verify** for the deeper check.

Choose the model on the same screen. A withdrawn model is exactly as fatal as a dead key.

## 8. Apify — blocked on a console click

Both registered actors return `full-permission-actor-not-approved` until someone approves them in
the Apify console. **Approval is per account, not per key**, so key rotation cannot fix it. Until
that click happens the integration delivers nothing, and the panel will report the reason verbatim
rather than mislabelling it as a bad credential.

---

## Optional: lock the Control plane to your network

Control plane → Configuration → **Control plane IP allowlist**. At most two entries; use a CIDR
range for an office.

Three safety properties, all deliberate:

- **Empty means open.** Default-closed would lock every existing deployment out of its own panel on
  upgrade.
- **A malformed list is ignored, not enforced.** The only place to fix a bad allowlist is the panel
  it would have closed.
- **The refusal names the address we observed.** Behind a proxy that is frequently not the one you
  expect, and without seeing it you cannot fix your own lockout.

Behind a reverse proxy this reads the first `X-Forwarded-For` entry. If the application is exposed
directly to the internet, do **not** rely on it — a client can set that header freely.

---

## Rollback

Migrations are additive only, so the previous image runs against the new schema. Roll the image
back and leave the database alone:

```bash
docker compose -f deploy/docker-compose.prod.yml up -d --no-deps app worker
```

To put the old plan ladder back on sale, set the retired plans to `active` in Admin → Plans. Their
rows and entitlements were never removed, and their subscribers never moved.
