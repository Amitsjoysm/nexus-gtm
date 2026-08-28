# 13 — Database access

How to reach the production and staging databases, why your laptop cannot connect directly, and
the exact commands for every operation you will actually need.

---

## Why `psql` from your laptop does not work, and is not going to

The PostgreSQL Flexible Server is created with:

```hcl
delegated_subnet_id           = azurerm_subnet.db.id   # injected into the VNet
public_network_access_enabled = false                  # no internet-facing endpoint
```

**These two are mutually exclusive in Azure.** A Flexible Server is either VNet-integrated
(private, reachable only from inside the virtual network) **or** it has a public endpoint with an
IP firewall. You cannot have both, and you cannot convert one into the other — switching networking
mode requires **building a new server and migrating the data**.

So there is no firewall rule to add for `203.199.234.154`. The server has no public endpoint for a
rule to apply to. Adding your IP is not a permission you are missing; it is an operation that does
not exist for this server.

That is the intended posture. The database is unreachable from the internet by construction, not by
configuration — which means it cannot be exposed by a misconfiguration either.

### What reaches it instead

| Path | Reaches DB because | Use for |
|---|---|---|
| `az containerapp exec` into `nexus-prod-app` | the container runs inside the VNet | **everything below** — day-to-day admin |
| A temporary container in the same ACA environment | same | `pg_dump` / `pg_restore` |
| Point-to-Site VPN gateway | puts your laptop inside the VNet | real `psql`, if you add one later (~$27+/mo) |

Access is gated by **Azure RBAC** on the Container App, not by an IP allowlist. That is stronger:
it is per-identity, revocable centrally, and every `exec` is recorded in the Azure Activity Log.

---

## Prerequisites

Run these once per shell. Everything below assumes them.

```bash
az login
az account set --subscription "<your-subscription-id>"

export RG=nexus-prod-rg
export APP=nexus-prod-app
```

For staging, substitute `RG=nexus-staging-rg` and `APP=nexus-staging-app`.

**Verify you can reach the app at all before anything else:**

```bash
az containerapp show --name "$APP" --resource-group "$RG" --query "properties.runningStatus" -o tsv
```

Expected output: `Running`. Anything else means fix the app first — none of the commands below can
work while the container is not up, because the container *is* your route to the database.

---

## Method 1 — an interactive shell (the normal way in)

```bash
az containerapp exec --name "$APP" --resource-group "$RG" --command /bin/sh
```

You land in `/app` as the non-root `nexus` user, with `NEXUS_DB_OWNER_URL` and `NEXUS_DATABASE_URL`
already in the environment.

**There is no `psql` in this container.** The image is `python:3.11-slim` and installs no
`postgresql-client` — see [deploy/Dockerfile](../../deploy/Dockerfile). What you have is Python
with `asyncpg` and SQLAlchemy, which is enough for every query you need. Use Method 2 for anything
requiring the real client tools.

Exit with `exit` or Ctrl-D.

### Which URL to use, and why it matters

| Variable | Role | RLS | Use for |
|---|---|---|---|
| `NEXUS_DATABASE_URL` | `nexus_app` | **enforced** (`NOBYPASSRLS`) | seeing what the application sees |
| `NEXUS_DB_OWNER_URL` | `nexus` (owner) | **bypassed** | migrations, cross-tenant reads, admin work |

Cross-tenant queries under `nexus_app` return **zero rows, not an error**. If a count comes back
`0` and you expected data, you almost certainly used the wrong URL — check that before concluding
the data is missing.

---

## Method 2 — `pg_dump` / `pg_restore` (a temporary client container)

**The application image cannot do this.** `docs/deployment/11-DESTROY-REBUILD.md` pipes `pg_dump`
through `containerapp exec` against `nexus-prod-app`; that command fails with `pg_dump: not found`,
and it fails at the worst possible moment — it is the backup you take immediately before destroying
the environment. Use this instead.

The fix is a throwaway container running the official Postgres image **inside the same Container
Apps environment**, which puts it inside the VNet and therefore in reach of the private server.

### Step 1 — read the owner connection string out of the app

```bash
DB_URL=$(az containerapp show --name "$APP" --resource-group "$RG" \
  --query "properties.template.containers[0].env[?name=='NEXUS_DB_OWNER_URL'].secretRef" -o tsv)

DB_URL=$(az containerapp secret show --name "$APP" --resource-group "$RG" \
  --secret-name "$DB_URL" --query value -o tsv)

# SQLAlchemy's driver suffix is not libpq syntax — strip it or psql rejects the URL.
PG_URL="${DB_URL/+asyncpg/}"
echo "${PG_URL%%:*}  (scheme only — the password is NOT printed)"
```

Expected output: `postgresql`. **Never echo `$PG_URL` itself** — it contains the owner password, and
Cloud Shell history persists in your `clouddrive`.

### Step 2 — start the client container

```bash
ENVID=$(az containerapp show --name "$APP" --resource-group "$RG" \
  --query "properties.managedEnvironmentId" -o tsv)

az containerapp create \
  --name nexus-dbclient --resource-group "$RG" \
  --environment "$ENVID" \
  --image postgres:16-alpine \
  --min-replicas 1 --max-replicas 1 \
  --cpu 0.25 --memory 0.5Gi \
  --secrets "dburl=$PG_URL" \
  --env-vars "PG_URL=secretref:dburl" \
  --command "sh" "-c" "sleep infinity" \
  --output none

echo "waiting for the client container..."
until [ "$(az containerapp show -n nexus-dbclient -g "$RG" --query properties.runningStatus -o tsv 2>/dev/null)" = "Running" ]; do sleep 5; done
echo "ready"
```

`postgres:16-alpine` matches the server major version. A client older than the server refuses to
restore a custom-format dump (`unsupported version in file header`), which is discovered at restore
time — during an incident.

### Step 3 — dump

```bash
az containerapp exec --name nexus-dbclient --resource-group "$RG" \
  --command "sh -c 'pg_dump \"\$PG_URL\" --no-owner --format=custom'" \
  > "nexus-$(date +%F).dump"

ls -lh nexus-*.dump
```

Expected: a file of non-trivial size (megabytes, not bytes).

**Check the file before trusting it.** `az containerapp exec` writes terminal control characters
into stdout on some CLI versions, which corrupts a binary dump silently — you get a plausible file
that will not restore, and you find out during the restore.

```bash
head -c 5 "nexus-$(date +%F).dump" | xxd | head -1
```

Expected: the bytes spell `PGDMP`. If they do not, dump to a file *inside* the container and copy
it out via Azure Blob Storage rather than piping through the terminal.

### Step 4 — DELETE THE CLIENT CONTAINER

```bash
az containerapp delete --name nexus-dbclient --resource-group "$RG" --yes
az containerapp list --resource-group "$RG" --query "[].name" -o tsv   # nexus-dbclient must be gone
```

It holds the owner password in a secret and can reach the database with full privileges. It is a
standing back door for as long as it exists. Delete it in the same session you created it.

---

## Restore

**Read [05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md) first.** Azure's automated backups with
point-in-time restore are the primary mechanism and are almost always the right tool — they restore
to a *new server*, leaving the original untouched, which a `pg_restore --clean` does not.

A `pg_restore --clean --if-exists` **drops and recreates every object it restores**. Against a live
production database that is a destructive operation with no undo. Do not run it on production
without an explicit decision and a fresh dump taken first.

```bash
# Steps 1-2 above, then:
az containerapp exec --name nexus-dbclient --resource-group "$RG" \
  --command "sh -c 'pg_restore --clean --if-exists --no-owner -d \"\$PG_URL\"'" \
  < nexus-2026-08-28.dump
```

After **any** restore, re-apply the role and RLS provisioning — a restore can reset ownership and
grants, and the API connects as `nexus_app`:

```bash
az containerapp exec --name "$APP" --resource-group "$RG" \
  --command "sh -c 'NEXUS_DATABASE_URL=\"\$NEXUS_DB_OWNER_URL\" python scripts/apply_rls.py'"
```

Skipping this leaves the API pointed at a role whose grants no longer exist. It fails closed
(errors, not silent data exposure), but it is a full outage until you notice.

---

## Common operations

All of these run inside `az containerapp exec ... --command /bin/sh` (Method 1).

### Confirm the schema is at Alembic head

```sh
alembic current
```

Expected: a revision id followed by `(head)`. If it names an older revision, migrations did not
complete — check the container startup logs.

### Count tenants and users (owner URL — crosses tenants)

```sh
python - <<'PY'
import asyncio, os, asyncpg
async def main():
    url = os.environ["NEXUS_DB_OWNER_URL"].replace("+asyncpg", "")
    c = await asyncpg.connect(url)
    for t in ("tenants", "users", "accounts", "signal_events"):
        print(f"{t:16} {await c.fetchval(f'select count(*) from {t}')}")
    await c.close()
asyncio.run(main())
PY
```

### Verify RLS is actually enforced

This is the single most valuable check in this file. It proves the tenant boundary is real rather
than assumed.

```sh
python - <<'PY'
import asyncio, os, asyncpg
async def main():
    # Connect as the APP role — the one the API uses — with no tenant GUC set.
    url = os.environ["NEXUS_DATABASE_URL"].replace("+asyncpg", "")
    c = await asyncpg.connect(url)
    n = await c.fetchval("select count(*) from accounts")
    print(f"accounts visible to nexus_app with NO tenant binding: {n}")
    print("PASS - RLS is enforcing." if n == 0 else "FAIL - RLS IS NOT ENFORCING. Investigate now.")
    r = await c.fetchrow("select rolbypassrls from pg_roles where rolname = current_user")
    print(f"current_user bypasses RLS: {r['rolbypassrls']}  (must be False)")
    await c.close()
asyncio.run(main())
PY
```

Expected: `0` accounts and `False`. **Anything else means every tenant can read every other
tenant's data** — treat it as a Sev-1 and stop deploying.

### Grant platform superadmin

```sh
python - <<'PY'
import asyncio, os, asyncpg
EMAIL = "you@infojoy.com"          # <-- change
async def main():
    url = os.environ["NEXUS_DB_OWNER_URL"].replace("+asyncpg", "")
    c = await asyncpg.connect(url)
    await c.execute("""
        insert into platform_admins (id, email, platform_role, permissions, active, created_at, updated_at)
        values (gen_random_uuid()::text, $1, 'superadmin', '[]'::jsonb, true, now(), now())
        on conflict (email) do update set active = true, platform_role = 'superadmin'
    """, EMAIL.lower())
    print(await c.fetch("select email, platform_role, active from platform_admins"))
    await c.close()
asyncio.run(main())
PY
```

An empty `permissions` list falls back to the role preset — see the platform-admin section of
`CLAUDE.md`. Do not hand-write a permission array unless you intend to pin it.

### Check connection headroom

The B1ms ceiling of roughly 50 connections is the binding constraint on this whole architecture,
and it is exceeded **during a rollout** rather than in steady state.

```sh
python - <<'PY'
import asyncio, os, asyncpg
async def main():
    c = await asyncpg.connect(os.environ["NEXUS_DB_OWNER_URL"].replace("+asyncpg",""))
    used = await c.fetchval("select count(*) from pg_stat_activity")
    cap  = await c.fetchval("show max_connections")
    print(f"{used} / {cap} connections in use")
    await c.close()
asyncio.run(main())
PY
```

Sustained usage above 40 means upsize the SKU **before** raising `app_min` — read the CONNECTION
BUDGET comment in [variables.tf](../../deploy/cloud/azure/variables.tf) first, because `app_min`,
the pool sizes and `pg_sku` are not independent knobs.

---

## Removing the deletion lock

Production carries **one** `CanNotDelete` lock, on the database server (see
[protection.tf](../../deploy/cloud/azure/protection.tf)). It blocks **deletion of that server
only** — every normal operation, migration and write is unaffected. `terraform destroy` fails
while it exists, deliberately.

> **There is no resource-group-scoped lock, and that is deliberate.** One was tried and broke the
> first deployment: at group scope, `CanNotDelete` applies to every child and blocks any operation
> ARM implements as a delete — including writing the service-association link that a VNet-injected
> PostgreSQL Flexible Server needs in its delegated subnet. The create failed with a message naming
> a *subnet* and a *delete* lock, during a database *create*. If you want the group locked, apply
> it by hand after the estate is built and remove it before any apply that alters the network.

**Removing these is the last safety net between a typo and total data loss.** Take a dump first
(Method 2), confirm it starts with `PGDMP`, and store it outside this subscription.

```bash
# Inspect what is locked
az lock list --resource-group "$RG" -o table

# Remove — only when destruction is genuinely intended.
# Note the --resource / --resource-type arguments: this lock is scoped to the SERVER, not the
# resource group, so a bare `az lock delete --name ... --resource-group ...` will not find it.
az lock delete --name gtm-prod-pg-nodelete --resource-group "$RG" \
  --resource "$(az postgres flexible-server list -g "$RG" --query '[0].name' -o tsv)" \
  --resource-type Microsoft.DBforPostgreSQL/flexibleServers
```

Re-apply them by running `terraform apply` against production — they are Terraform-managed and will
be recreated.

---

## Troubleshooting

**`az containerapp exec` hangs, or exits immediately**

The command needs an interactive-capable terminal. It does not work from most CI agents, and it can
fail in a Cloud Shell tab that has been idle long enough to lose its websocket.

```bash
az extension update --name containerapp     # the exec transport lives in the extension
az containerapp exec -n "$APP" -g "$RG" --command /bin/sh
```

If it still hangs, reload Cloud Shell. If it fails only from a pipeline, that is expected — use a
Container Apps **job** for automation rather than `exec`.

---

**`could not translate host name "...postgres.database.azure.com"`**

You are running the command outside the VNet — from Cloud Shell directly, rather than through
`containerapp exec`. The private DNS zone resolves only inside the VNet. Confirm where you are:

```bash
hostname    # a container name means you are inside; a Cloud Shell name means you are not
```

---

**`permission denied for table ...` or a query returning 0 rows unexpectedly**

You are connected as `nexus_app`, which has RLS enforced and no tenant binding. Use
`NEXUS_DB_OWNER_URL` for cross-tenant work. This is the documented trap and it has bitten this
codebase repeatedly — a cross-tenant read under the app role returns **zero rows, not an error**.

---

**`pg_dump: not found`**

Expected. The application image has no Postgres client tools. Use Method 2.

---

**`az lock delete` returns `AuthorizationFailed`**

Deleting a lock needs `Microsoft.Authorization/locks/delete`, which Contributor does **not** grant.
You need Owner or User Access Administrator. That is the point of the lock — it cannot be removed by
the same role that performs routine deployments.

---

## What is deliberately not here

- **No public database endpoint, and no path to one.** Switching networking mode requires rebuilding
  the server. If you decide you need it, that is a migration with an outage, planned in advance —
  not a firewall change.
- **No shared admin credentials.** Access is per-identity through Azure RBAC. Revoke by removing the
  person's role assignment on the Container App, not by rotating a password everyone knows.
- **No permanent client container.** Method 2 creates one and deletes it in the same session.
