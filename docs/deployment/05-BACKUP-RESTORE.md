# 05 — Backup, restore and what is *not* recoverable

## What is protected, and what is not

| Data | Protection | Recoverable? |
|---|---|---|
| **Postgres** — all tenant data | Automatic backups + PITR | ✅ to any second in the retention window |
| **Terraform state** | Blob versioning + 30-day soft delete | ✅ to any prior version |
| **Container images** (ACR) | None beyond the registry itself | ⚠️ rebuildable from source |
| **`deploy/.env`** | **Nothing.** You are the backup. | ❌ **lost is lost** |
| **Valkey queue contents** | **None — deliberately ephemeral** | ❌ see below |
| **Log Analytics** | Retention window only | ⚠️ ages out |

Two rows deserve their own sections.

---

## `deploy/.env` — the one thing nobody else can restore

It holds your Postgres passwords, `NEXUS_SECRET_KEY`, and every provider API key. It is gitignored,
never in the image, never in an upload bundle. **If you lose it:**

- `NEXUS_SECRET_KEY` gone ⇒ every JWT is invalid ⇒ every user logged out
- `NEXUS_APP_DB_PASSWORD` gone ⇒ recoverable, `apply_rls.py` resets it on next boot
- `POSTGRES_PASSWORD` gone ⇒ resettable via `az postgres flexible-server update --admin-password`
- API keys gone ⇒ re-paste from each provider console

Cloud Shell home directories **do get wiped** — this happened during the initial deployment.

```bash
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M)
```

`~/clouddrive` is an Azure Files share and survives session resets, but it is still one account.
**Download a copy** via Cloud Shell **⇅ → Download**, and put it in a password manager or a
secrets vault. Do it now if you have not.

## Valkey — ephemeral on purpose

Valkey runs with `--save ''` and no AOF, on ephemeral container storage. **A restart loses whatever
was queued at that instant.** Restarts happen on every deploy, on platform maintenance, and on OOM.

This is a deliberate trade — the alternative was Azure Managed Redis at $40–300/mo against a
~$90/mo budget. What makes it survivable is the durability design, not luck:

- **Periodic sweeps re-enqueue themselves every tick and are idempotent** — account refresh, cadence
  steps, billing rollups all self-heal within one interval
- **Handler failures are dead-lettered to Postgres** (`dead_letter_jobs`), so anything that ran and
  failed is durable and replayable
- **Idempotency keys are short-TTL**, so losing them at worst permits one duplicate retry

**What is genuinely lost:** one-shot jobs in flight at the moment of restart — a campaign send, an
orchestration run, a single account refresh. Bounded, not zero.

Check the queue before a deliberate restart:

```bash
az containerapp exec --name nexus-prod-app --resource-group nexus-prod-rg \
  --command "python -c \"
import os, redis
r = redis.from_url(os.environ['NEXUS_REDIS_URL'])
print('queue depth:', sum(r.llen(k) for k in r.keys('nexus:*')))\""
```

Deep queue ⇒ wait for it to drain. Upgrade trigger: if depth is routinely non-trivial, move to
Azure Managed Redis (see [06-SCALING.md](06-SCALING.md)).

---

## Postgres backups

Automatic, on by default, `backup_retention_days = 14`.

```bash
az postgres flexible-server show --name nexus-prod-pg-v3 --resource-group nexus-prod-rg \
  --query "{retention:backup.backupRetentionDays, geoRedundant:backup.geoRedundantBackup, earliest:backup.earliestRestoreDate}" -o jsonc
```

`geoRedundantBackup: Disabled` is the Stage 0 choice — it protects against a full regional loss and
roughly doubles backup cost. Enable it when the business case justifies regional DR; it can only be
set **at creation time**, so switching later means a restore into a new server.

### Point-in-time restore

PITR creates a **new server**; it never overwrites the existing one. That is the right behaviour —
you get to compare before cutting over.

```bash
az postgres flexible-server restore \
  --name nexus-prod-pg-restored \
  --resource-group nexus-prod-rg \
  --source-server nexus-prod-pg-v3 \
  --restore-time "2026-08-06T09:00:00Z"
```

Then point the app at it by updating `local.pg_fqdn`, or swap names. **Test the restored copy
before cutting over** — a restore you have never exercised is a hope, not a plan.

### Manual logical dump

Useful before a risky migration, and it is the only backup you can take *off* Azure.

```bash
az containerapp exec --name nexus-prod-app --resource-group nexus-prod-rg \
  --command "sh -c 'pg_dump \"\$NEXUS_DB_OWNER_URL\" --no-owner --format=custom' " > nexus-$(date +%F).dump
```

If `pg_dump` is absent from the image, run it from Cloud Shell against the server FQDN — Postgres is
on a private subnet, so this needs the VNet, a jump host, or temporarily enabling public access with
a firewall rule (turn it back off immediately).

Restore:

```bash
pg_restore --clean --if-exists --no-owner -d "$NEXUS_DB_OWNER_URL" nexus-2026-08-06.dump
```

---

## Terraform state

Versioning and 30-day soft delete are enabled on the storage account. To recover a corrupted state:

```bash
az storage blob list --container-name tfstate --account-name <SA> --include v \
  --query "[].{name:name, version:versionId, modified:properties.lastModified}" -o table
```

```bash
az storage blob copy start --destination-container tfstate \
  --destination-blob nexus/prod.tfstate \
  --source-uri "https://<SA>.blob.core.windows.net/tfstate/nexus/prod.tfstate?versionid=<VERSION>"
```

Then `terraform plan` and confirm it proposes **no changes** before doing anything else.

---

## Disaster scenarios

| Scenario | Recovery | Data loss | Time |
|---|---|---|---|
| App container crashes | ACA restarts automatically | none | seconds |
| App revision bad | Redeploy previous image tag | none | ~5 min |
| Valkey restarts | Automatic | in-flight one-shot jobs | seconds |
| Worker dies | ACA restarts; sweeps self-heal | none | ~1 min |
| Postgres corruption | PITR to a new server | to chosen timestamp | 30–60 min |
| Accidental `terraform destroy` | Rebuild + PITR restore | none if backups intact | 1–2 hours |
| Region outage | Rebuild elsewhere from source + dump | since last dump | hours |
| **`.env` lost** | **Rotate everything, all users logged out** | credentials | hours |
| **Subscription deleted** | Nothing survives | everything | — |

The last two are the ones with no technical remedy. Both are solved the same way: keep `.env` and a
periodic `pg_dump` somewhere that is not this Azure subscription.

## Minimum viable backup routine

Weekly, five minutes:

```bash
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d)
az postgres flexible-server show --name nexus-prod-pg-v3 --resource-group nexus-prod-rg \
  --query "backup.earliestRestoreDate" -o tsv
az storage blob list --container-name tfstate --account-name <SA> -o table | tail -3
```

Confirms the credentials are backed up, the PITR window is real, and state is being written.
