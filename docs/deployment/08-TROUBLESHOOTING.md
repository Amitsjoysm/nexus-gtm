# 08 — Troubleshooting

Every entry below was hit on a real deployment. The pattern worth internalising: **most of these
error messages name the wrong thing.** Read the "real cause" column before acting on the symptom.

---

## The seventeen failures, and what they actually meant

| Symptom | What it looks like | Real cause | Fix |
|---|---|---|---|
| `Version should be in: []` | PostgreSQL 16 unsupported | Provider unregistered **or region restricted** | Register providers; check `list-skus` byte count |
| `MissingSubscriptionRegistration` | — | `Microsoft.App` unregistered | `az provider register --wait` |
| `404 container does not exist` | State account broken | Blob **container** never created | Create `tfstate` container |
| `name ... already in use` (ACR) | — | Names are globally unique | `name_suffix` |
| `Name unavailable for reservation` | — | Azure **reserves Redis names after deletion** | New `name_suffix`; old name may need a support ticket |
| `Azure Cache for Redis is retiring` | — | Resource **type** refused, not an argument | Valkey Container App |
| `interface {} is nil, not string` | Provider crash | Empty string in an `args` list | `command = ["sh","-c","..."]` |
| `zone can only be changed when...` | — | Azure assigned a zone; config says null | `lifecycle { ignore_changes = [zone] }` |
| `permission denied to alter role` | — | Managed admin is **not superuser** | Fallback + verify in `apply_rls.py` |
| Environment replaced every apply | Deploy "works" | `infrastructure_resource_group_name` server-populated | `ignore_changes` |
| `AuthorizationPermissionMismatch` | — | Contributor ≠ blob **data** access | Use the account key |
| `CHANGE_ME` becomes a live password | Silent | Truthiness test treated it as a value | Placeholder guard |
| Alerts never arrive | Rule visible in portal | `alarm_email` empty ⇒ zero receivers | Pass `ALARM_EMAIL` |
| No signup email | Form waits forever | UI always uses OTP; no SMTP | See [03](03-SEED-USERS.md) |
| `Unsupported argument: non_ssl_port_enabled` | — | azurerm 4.x name on a 3.x pin | Use `enable_non_ssl_port` |
| `docker: command not found` | — | Cloud Shell has no Docker daemon | `az acr build` |
| Validator rejects `NEXUS_APP_DB_PASSWORD` | Deploy blocked | `TF_VAR_secrets` lost on reconnect | `source ~/clouddrive/nexus-env.sh` |

---

## `405 Method Not Allowed` on an API call

**Every application route is mounted under `/api`** (`nexus/main.py:204`). Only `/health`,
`/ready`, `/metrics`, `/docs` and `/openapi.json` live at the root.

A request to `/auth/signup` therefore falls through to the SPA static mount, which is registered
last as a catch-all and serves GET only — so POST comes back `405`, not `404`. That reads as
"the endpoint exists but rejects POST", which sends you looking at the route definition instead of
the path.

```bash
curl -s -X POST "$URL/api/auth/signup" ...   # correct
```

Authoritative list from the running app:

```bash
curl -s "$URL/openapi.json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p,o in sorted(d['paths'].items()): print(' '.join(m.upper() for m in o), p)"
```

Related: `Azure Container App - Unavailable` (HTML 404) means the **hostname** is a retired
revision FQDN, not that the app is down. Use the stable ingress FQDN:

```bash
az containerapp show -n nexus-prod-app -g nexus-prod-rg   --query properties.configuration.ingress.fqdn -o tsv
```

## `/ready` never returns 200

`/health` 200 + `/ready` non-200 means the process is up but the database is not usable.

```bash
az containerapp logs show --name nexus-prod-app --resource-group nexus-prod-rg --tail 100
```

Read the entrypoint sequence — where it stops tells you the cause:

```
[entrypoint] waiting for the database to accept connections...
[entrypoint] database <fqdn>:5432 is up          ← network + DNS fine
[bootstrap] stamped database: alembic upgrade head
[bootstrap] done                                  ← schema at head
[apply_rls] waiting for the provisioning lock...
[entrypoint] database ready.                      ← everything worked
```

| Stops at | Cause |
|---|---|
| `waiting for the database` | Private DNS or subnet delegation broken |
| `bootstrapping` with a traceback | Migration failure — read the alembic error |
| `[apply_rls] FAILED` | Role/privilege problem — see below |
| Reaches `database ready` but `/ready` still fails | App-level; check for a config validator raising |

## `[apply_rls] FAILED: permission denied to alter role`

Azure's admin is a member of `azure_pg_admin`, **not** a superuser, and Postgres refuses
`NOSUPERUSER`/`NOBYPASSRLS` from a non-superuser *even when setting them to the value the role
already has*.

Fixed in `scripts/apply_rls.py`: it tries the explicit form, falls back to what a managed admin can
set, then **verifies against `pg_roles`** that `rolsuper` and `rolbypassrls` are both false — and
fails the deploy if not.

> The lazy fix is deleting the two attributes. Don't. They decide whether RLS is a real tenant
> boundary or decoration, and a role with `BYPASSRLS` ignores every policy while nothing reports it.
> Verify; do not assume.

Requires an **image rebuild** — `scripts/` is baked into the container.

## App cannot reach Valkey

```bash
az containerapp exec --name nexus-prod-app --resource-group nexus-prod-rg \
  --command "python -c \"
import os, redis
print('url:', os.environ['NEXUS_REDIS_URL'])
print('ping:', redis.from_url(os.environ['NEXUS_REDIS_URL']).ping())\""
```

`Connection refused` / DNS failure ⇒ the bare app name is not resolving. Fall back to the full
internal FQDN in `container_apps.tf`:

```hcl
redis_host = azurerm_container_app.valkey.ingress[0].fqdn
```

Also confirm Valkey is up and that ingress is `transport = "tcp"`, `exposed_port = 6379`:

```bash
az containerapp show -n nexus-prod-valkey -g nexus-prod-rg \
  --query "{running:properties.runningStatus, ingress:properties.configuration.ingress}" -o jsonc
```

## Users log in, then get logged out

`NEXUS_SECRET_KEY` changed. Every JWT is signed with it. Most likely a re-run of the secret
generator, or `.env` was restored from a backup taken before the secrets were baked.

Restore the original `.env` from `~/clouddrive/nexus-env-backup-*` and re-apply. If it is genuinely
lost, everyone re-authenticates once — annoying, not fatal.

## Everything is 500 after a deploy

```bash
az containerapp logs show --name nexus-prod-app --resource-group nexus-prod-rg --tail 200 \
  | grep -A20 Traceback | head -40
```

Then roll back to the previous tag ([07-OPERATIONS.md](07-OPERATIONS.md#rolling-back)). Roll back
first, diagnose second — a restored service buys you time to read the traceback properly.

## Terraform wants to destroy something unexpected

**Stop. Do not approve.**

```bash
terraform plan -no-color -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG" 2>&1 | grep -B5 -A40 "must be replaced" | grep "forces replacement"
```

The `# forces replacement` line names the attribute. If it is a field Azure populates server-side,
add it to a `lifecycle { ignore_changes = [...] }` block. Three already exist for exactly this:
`zone` (Postgres), `infrastructure_resource_group_name` (environment),
`workload_profile_name` (container apps).

## Terraform state is locked

```bash
terraform force-unlock <LOCK_ID>
```

Only after confirming no other apply is running. Breaking a live lock lets two applies interleave.

---

## Do's and don'ts

### Do

- **`source ~/clouddrive/nexus-env.sh` at the start of every session**
- **Read the plan header before every `yes`** — specifically the destroy count
- **Use a fresh image tag every deploy** — `:latest` twice is a no-op
- **Back up `.env` after every change**, and keep a copy off Azure
- **Check `/ready`, not `/health`** — only one of them proves anything
- **Bump `name_suffix` when rebuilding from scratch**
- **Verify the region's SKU catalog before choosing it**
- **Fix software before buying hardware** — profile the query first

### Don't

- **Don't edit env vars in the Azure portal** — Terraform reverts them silently
- **Don't rotate `NEXUS_SECRET_KEY`** unless you mean to log everyone out
- **Don't point the app at `NEXUS_DB_OWNER_URL`** to "fix" a permission error — that removes RLS for
  every tenant with no error and no log line. It is the single most damaging change available here.
- **Don't interrupt a `terraform apply` mid-create** — leaves resources Azure knows about that state
  does not
- **Don't restart Valkey without checking queue depth**
- **Don't approve a plan that replaces the ACA environment** — it cascades to every app in it
- **Don't run `seed_demo.py` against production** — it creates a real workspace with a password
  published in this repository
- **Don't enable `NEXUS_OTP_REGISTRATION_ENABLED` before SMTP is verified** — it closes
  `/auth/signup` too, locking out both paths
- **Don't delete `nexus-tfstate-rg`** — it is the record of everything else

---

## Escalation order

1. `/health` and `/ready` — is it up at all?
2. `az containerapp logs show --tail 100` — what does it say?
3. `az containerapp revision list` — crash loop?
4. `terraform plan` — has reality drifted from intent?
5. Roll back to the last known-good tag
6. Postgres PITR — only if data is actually damaged

Steps 1–4 are read-only and safe under pressure. Step 5 is the fastest real fix. Step 6 is a last
resort and takes 30–60 minutes.
