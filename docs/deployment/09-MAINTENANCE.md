# 09 — Maintenance, upgrades and migration

## Database migrations

Alembic, under `migrations/versions/`. **Additive only** — that is a hard rule, not a preference,
and three things depend on it:

- A rollback to a previous image must keep working against the newer schema
- Container Apps runs old and new revisions simultaneously during a rollout
- `tests/test_migrations_replay.py` builds a database from nothing but `alembic upgrade head` and
  diffs it against `Base.metadata`

Migrations run **automatically on app boot** (`NEXUS_RUN_MIGRATIONS=1` on the app only — the worker
deliberately skips them to avoid a race).

### Adding one

```bash
alembic revision -m "add_thing"
# edit the generated file — additive changes only
pytest tests/test_migrations_replay.py -q
```

Then deploy normally. Watch the logs for `[bootstrap] done`.

> **Never reintroduce `create_all()` inside a revision.** The old `0001_initial` did that, so it
> materialised whatever models existed *at run time* rather than a frozen historical schema — and
> the chain could never be replayed. That is why `0020_baseline_schema` is a frozen literal-DDL
> squash.

### Two traps that only bite against real Postgres

The test suite runs on SQLite, which has **no RLS**. Both of these pass every test and fail in
production:

1. **A hand-built `TenantSession` must call `apply_rls(session, tenant_id)` first**, or writes are
   rejected. `tests/test_rls_binding_guard.py` is an AST guard against this.
2. **Cross-tenant reads return ZERO ROWS, not an error.** Use `get_platform_sessionmaker()` (owner
   role) for genuinely cross-tenant work, and only behind `require_platform_admin` or signature
   verification.

Also: `apply_rls` sets the tenant GUC **transaction-locally**, so a read after `commit()` has no
binding and silently returns nothing.

### If a migration fails mid-deploy

The app crash-loops; the previous revision keeps serving until the new one is healthy, so you are
usually still up.

```bash
az containerapp logs show --name nexus-prod-app --resource-group nexus-prod-rg --tail 100 \
  | grep -A20 "bootstrapping"
```

Roll back the image, fix the migration, redeploy. If the schema is genuinely damaged, PITR to just
before the deploy ([05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md)).

---

## Upgrading the Terraform provider

Currently pinned `~> 3.110`, resolving to 3.117.x. **Moving to 4.x is not a version bump** — it
renames arguments and changes provider requirements.

Known coupling, and there will be more:

| 3.x | 4.x |
|---|---|
| `enable_non_ssl_port` | `non_ssl_port_enabled` |
| provider block without `subscription_id` | `subscription_id` required |

Do it deliberately: change the pin, `terraform init -upgrade`, `terraform validate`, then **read the
plan very carefully** for anything marked `must be replaced`. On a stateful stack a provider upgrade
that proposes replacing Postgres is a data-loss event, not a refactor.

## Upgrading PostgreSQL major version

`pg_version` is a variable, but **changing it on a live server does not perform an in-place
upgrade** — Terraform will propose replacing the server, which destroys the data.

The safe path:

1. `pg_dump` the current database
2. Create a new server at the target version
3. `pg_restore` into it
4. Repoint `local.pg_fqdn`, verify, then decommission the old one

Azure also offers in-place major-version upgrade for Flexible Server — prefer that where available,
and take a dump first regardless.

## Rotating credentials

```bash
# Postgres admin
az postgres flexible-server update --name nexus-prod-pg-v3 --resource-group nexus-prod-rg \
  --admin-password "$(python3 -c 'import secrets;print(secrets.token_hex(24))')"
# then update POSTGRES_PASSWORD in .env and re-apply
```

`NEXUS_APP_DB_PASSWORD` is simpler — change it in `.env` and re-apply; `apply_rls.py` reconciles the
role password on every boot.

Storage account key (do this once after the initial deploy, since it gets echoed into shell history
during setup):

```bash
az storage account keys renew --account-name <SA> --resource-group nexus-tfstate-rg --key key1
```

**Not during a deploy** — it breaks Terraform's backend connection mid-apply.

⚠️ `NEXUS_SECRET_KEY` rotation logs out every user in every workspace, instantly.

## Adding a custom domain

Additive — no rebuild.

```bash
cd ~/nexus/deploy/cloud/azure && terraform output app_fqdn
```

1. CNAME `app.yourdomain.com` → that FQDN
2. Add a TXT record `asuid.app` with the value of `custom_domain_verification_id`
3. Add `azurerm_container_app_custom_domain` + a managed certificate to `container_apps.tf`
4. Apply

The Microsoft-managed certificate is free and auto-renews. Until then the app serves on the
`*.azurecontainerapps.io` hostname with a valid certificate — there is no urgency here.

## Migrating regions

Everything is regional and Postgres must share a region with its delegated subnet, so this is a
**full rebuild**, not a move.

1. `pg_dump` (and confirm the dump restores somewhere)
2. Verify the target region's SKU catalog — see [01-LAUNCH.md](01-LAUNCH.md#step-0--preflight-verify-the-region-before-anything-else)
3. New `name_suffix`, apply into the new region
4. `pg_restore`
5. Cut DNS over, then destroy the old stack

## Migrating clouds

`deploy/cloud/aws/` mirrors this stack on ECS Fargate. The application is unchanged — same image,
same `deploy/.env`, same migrations. What differs is the infrastructure layer and the queue
(ElastiCache vs. the Valkey container).

Nothing in the app is Azure-specific. That is by design and worth preserving.

---

## Keeping the code-review graph current

The indexer only sees **git-tracked** files. A brand-new file is invisible until at least
`git add`ed — including to a full `build`.

```bash
git add -N .
code-review-graph build
code-review-graph search "SomeNewSymbol"     # verify, don't trust the summary line
```

Incremental `update` re-indexes *modified* tracked files only, so it silently misses new ones even
when they are staged.

## Routine maintenance calendar

| Cadence | Task |
|---|---|
| Weekly | `terraform plan` (want: no changes) · `/ready` check · back up `.env` |
| Monthly | Review costs · check PITR window · `az acr repository show-tags` and prune old images |
| Quarterly | Rotate credentials · review scaling triggers · test a restore |
| As needed | Provider upgrade · Postgres major version · region change |

The quarterly **restore test** is the one people skip. A backup you have never restored is a
hypothesis. Restore into a scratch server, point a local app at it, confirm it works, delete it.

## The five things that will hurt most if forgotten

1. **`.env` has no backup but yours.** Off Azure, in a password manager.
2. **A clean weekly `terraform plan` is your best health signal** — it catches drift before it
   becomes an incident.
3. **Migrations are additive-only.** One destructive migration breaks rollback, and you find out
   during an incident.
4. **RLS is enforced by the role, not the code.** Anything that connects the API as the owner
   removes tenant isolation with no error.
5. **Region and global names are decided once.** Both are rebuild-level changes.
