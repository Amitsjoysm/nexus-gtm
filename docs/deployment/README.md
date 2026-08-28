# NEXUS GTM — Azure operations

Everything needed to launch, verify, operate and scale the Azure deployment.

Written **after** a real first deployment, not before one. **Twenty-five distinct issues** were hit
getting from an empty subscription to a usable product; every one is fixed in code and recorded in
[10-ISSUE-LOG.md](10-ISSUE-LOG.md) with its symptom, its real cause, and the guard that prevents it.

**In 14 of those 25 cases the error message named the wrong thing.** `Version should be in: []`
meant "this region is capacity-restricted". `405 Method Not Allowed` meant "wrong URL prefix".
`permission denied to alter role` meant "managed Postgres admins are not superusers". Reading
symptoms literally cost more time than any single fix — which is why the issue log records what
each message *looked* like alongside what it *was*.

Start with [01-LAUNCH.md](01-LAUNCH.md) to build, [11-DESTROY-REBUILD.md](11-DESTROY-REBUILD.md) to
rebuild, and [10-ISSUE-LOG.md](10-ISSUE-LOG.md) or
[08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md) when something breaks.

## Read in this order

| Doc | Use when |
|---|---|
| [01-LAUNCH.md](01-LAUNCH.md) | Building from scratch — through to a **usable** site, not just running infra |
| [02-SMOKE-TESTS.md](02-SMOKE-TESTS.md) | Immediately after a deploy — is it actually working? |
| [03-SEED-USERS.md](03-SEED-USERS.md) | Creating the first workspace, superadmin and users |
| [04-ENDPOINT-TESTS.md](04-ENDPOINT-TESTS.md) | Verifying the API surface end to end |
| [05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md) | Backups, PITR, what is and is not recoverable |
| [06-SCALING.md](06-SCALING.md) | Traffic grew — what to change, in what order |
| [07-OPERATIONS.md](07-OPERATIONS.md) | Day-to-day: deploys, logs, secrets, cost |
| [08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md) | Something is broken |
| [09-MAINTENANCE.md](09-MAINTENANCE.md) | Upgrades, migrations, moving regions or clouds |
| [10-ISSUE-LOG.md](10-ISSUE-LOG.md) | **Every issue hit and how it was resolved** — read when anything breaks |
| [11-DESTROY-REBUILD.md](11-DESTROY-REBUILD.md) | **Exact copy-paste commands** to tear down and rebuild to fully live |
| [12-RESOURCE-INVENTORY.md](12-RESOURCE-INVENTORY.md) | **Every resource and file that keeps the site alive** — purpose, cost, what breaks without it |

## The deployment in one picture

```
                      Internet
                         │  HTTPS (Microsoft-managed cert)
                ┌────────▼─────────────────────────────────┐
                │  Container Apps environment (VNet)       │
                │                                          │
                │   nexus-prod-app      external ingress   │
                │     FastAPI + React SPA, 1-3 replicas    │
                │     runs migrations + RLS on boot        │
                │            │                    │        │
                │            │ redis://           │        │
                │   nexus-prod-valkey  internal TCP :6379  │
                │     queue + idempotency, exactly 1       │
                │            │                             │
                │   nexus-prod-worker  no ingress          │
                │     queue consumer + scheduler, 1        │
                └────────────┼─────────────────────────────┘
                             │ private, delegated subnet
                   PostgreSQL Flexible Server (B1ms)
                     RLS-enforced, nexus_app least-priv role
```

Also present: ACR (image registry), Log Analytics, an action group + restart alert, a VNet with
three subnets, and a private DNS zone for Postgres.

## The five facts that explain most decisions

1. **The database connection budget is the binding constraint.** B1ms allows ~50 connections;
   peak usage doubles during a rollout. That single number decides `app_min`, the pool sizes, and
   the Postgres SKU. See [06-SCALING.md](06-SCALING.md).
2. **Region matters more than it looks.** PostgreSQL Flexible Server provisioning is *restricted*
   in some regions per subscription, and Azure reports that as an empty version list. Everything is
   regional, so changing it means rebuilding the whole stack.
3. **Azure Cache for Redis is retired.** Valkey runs as a Container App instead. It has no
   persistence — see the durability trade in [05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md).
4. **Several Azure fields are server-populated**, and Terraform will fight them forever without
   `lifecycle { ignore_changes }`. One of those silently destroys the whole runtime on every apply.
5. **The API lives under `/api`.** Only `/health`, `/ready`, `/metrics` and `/docs` are at the
   root. A root-path API call hits the SPA catch-all and returns `405`, not `404`.
6. **RLS is the tenant boundary, and it is enforced by a role, not by application code.** Anything
   that lets the API connect as the owner removes tenant isolation with no error and no log line.
