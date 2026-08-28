# 07 — Day-to-day operations

## The session helper (do this first, every time)

`TF_VAR_secrets` is a shell variable and dies with your Cloud Shell session. Terraform's validator
**refuses to run without it** — including on destroy — which is a feature, not friction: without it
Terraform would point the app at a role `apply_rls.py` never created.

Create the helper once:

```bash
cat > ~/clouddrive/nexus-env.sh <<'EOS'
# source ~/clouddrive/nexus-env.sh — restores deploy vars after a Cloud Shell reconnect.
# Reads deploy/.env; holds no secrets itself.
export TF_VAR_secrets="$(python3 - ~/nexus/deploy/.env <<'PY'
import json, sys
env = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
skip = {"NEXUS_ENV","NEXUS_QUEUE_BACKEND","NEXUS_RUN_MIGRATIONS","DOMAIN","ACME_EMAIL",
        "NEXUS_DATABASE_URL","NEXUS_DB_OWNER_URL","NEXUS_WORKER_DATABASE_URL","NEXUS_REDIS_URL"}
print(json.dumps({k: v for k, v in env.items() if k not in skip and v}))
PY
)"
export REG="nexusprodacrv3.azurecr.io"
export RG="nexus-prod-rg"
export SFX="v3"
echo "restored: $(echo "$TF_VAR_secrets" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') secrets"
EOS
```

Then at the start of every session:

```bash
source ~/clouddrive/nexus-env.sh
```

---

## Deploying a code change

```bash
source ~/clouddrive/nexus-env.sh
cd ~/nexus && export TAG="$(date +%Y%m%d-%H%M)"
az acr build --registry "${REG%%.*}" --image "nexus:$TAG" --image "nexus:latest" \
  --file deploy/Dockerfile .
cd deploy/cloud/azure
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG"
```

**Always use a fresh timestamp tag.** ACA will not pull a changed `:latest` on its own — the image
reference is identical, so nothing tells it to. Deploying with `:latest` twice appears to succeed
and changes nothing.

**Record the tag.** It is your only durable rollback handle; `:latest` is overwritten on every
build.

### Before you type `yes`

Read the plan header. You want `0 to destroy`.

> **If `azurerm_container_app_environment.main must be replaced` appears, STOP.** Replacing the
> environment cascades — every Container App in it is replaced too, because
> `container_app_environment_id` becomes "known after apply". That is a full outage of the app,
> worker and queue. It should be prevented by the `lifecycle { ignore_changes }` block in
> `platform.tf`; if it reappears, a new server-populated field is fighting the config.

## Rolling back

```bash
az acr repository show-tags --name "${REG%%.*}" --repository nexus --orderby time_desc -o table
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:<PREVIOUS_TAG>"
```

**A rollback does not revert database migrations.** Migrations in this repo are additive-only
(see `CLAUDE.md`), so the previous image runs correctly against the newer schema — but verify
rather than assume, especially if the release you are backing out added a column the old code
does not know about.

## Changing a secret or API key

Never edit env vars in the Azure portal. Terraform owns those resources and the next apply silently
reverts them — the classic drift trap.

```bash
code ~/nexus/deploy/.env                    # edit
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M)
source ~/clouddrive/nexus-env.sh            # REQUIRED — re-reads .env
cd ~/nexus/deploy/cloud/azure
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" -var "image=$REG/nexus:$TAG"
```

Creates a new revision and rolls it out — ~2 minutes, no rebuild.

⚠️ **Never rotate `NEXUS_SECRET_KEY` casually.** It signs every JWT; changing it logs out every user
in every workspace instantly.

## Logs

```bash
az containerapp logs show --name nexus-prod-app --resource-group $RG --tail 100
az containerapp logs show --name nexus-prod-app --resource-group $RG --follow      # live
az containerapp logs show --name nexus-prod-worker --resource-group $RG --tail 100
```

Structured search over the retention window:

```bash
WS=$(az monitor log-analytics workspace show -g $RG -n nexus-prod-logs --query customerId -o tsv)
az monitor log-analytics query --workspace "$WS" --analytics-query "
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h) and ContainerAppName_s == 'nexus-prod-app'
| where Log_s contains 'ERROR' or Log_s contains 'Traceback'
| project TimeGenerated, Log_s | order by TimeGenerated desc | take 50" -o table
```

## Shell into a container

```bash
az containerapp exec --name nexus-prod-app --resource-group $RG --command /bin/sh
```

Useful inside: `python -c "from nexus.core.config import get_settings; print(get_settings().env)"`,
`env | grep NEXUS_ | cut -d= -f1` (names only — never print values).

## Restarting

```bash
az containerapp revision restart --name nexus-prod-app --resource-group $RG \
  --revision "$(az containerapp show -n nexus-prod-app -g $RG --query properties.latestRevisionName -o tsv)"
```

⚠️ **Restarting Valkey drops the queue.** Check depth first — see
[05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md#valkey--ephemeral-on-purpose).

## Cost control

```bash
az consumption usage list --start-date $(date -d '30 days ago' +%Y-%m-%d) --end-date $(date +%Y-%m-%d) \
  --query "[?contains(instanceName,'nexus')].{r:instanceName,cost:pretaxCost}" -o table | tail -20
```

Set a budget: **Portal → Cost Management → Budgets** → scope `nexus-prod-rg` → alerts at 50/80/100%.

Cap log ingestion — it is the line item that surprises people, since a crash loop can generate
gigabytes in hours:

```bash
az monitor log-analytics workspace update -g $RG -n nexus-prod-logs --quota 1
```

1 GB/day. Ingestion is the cost driver, not retention.

## Weekly checklist

```bash
source ~/clouddrive/nexus-env.sh
cd ~/nexus/deploy/cloud/azure

terraform plan -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG" 2>&1 | grep -E "^Plan:|must be replaced"   # want: no changes
curl -s -o /dev/null -w "ready: %{http_code}\n" "$(terraform output -raw app_default_url)/ready"
az postgres flexible-server show -n nexus-prod-pg-v3 -g $RG --query "storage.storageSizeGb" -o tsv
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d)
```

A clean weekly `terraform plan` with **no changes** is the single best health signal you have — it
means reality matches intent, and it catches drift before it becomes an incident.

## Alerts

One metric alert ships: app restart count > 3 in 5 minutes.

> **It notifies nobody unless `alarm_email` is set.** The action group is created either way, so the
> portal shows an alert rule and it reads as configured. Verify:

```bash
az monitor action-group show -n nexus-prod-alerts -g $RG --query "emailReceivers[].emailAddress" -o tsv
```

Empty output means alerts go nowhere. Fix by re-applying with `-var "alarm_email=..."`.
