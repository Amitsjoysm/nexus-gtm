# 14 — Azure deployment guide (Cloud Shell, start to finish)

**Deploying `gtm.infojoy.com` from an empty Azure subscription, using only the Azure terminal.**

Every command is copy-paste ready and states what it does, what you should see, how to verify it,
and what to do when it fails. No Docker, no local tooling — Azure Cloud Shell has everything.

> **Read this box before you start.**
>
> - Total hands-on time is roughly **90 minutes**, of which ~35 is waiting on Azure.
> - You will create **two environments**: `nexus-prod-rg` and `nexus-staging-rg`.
> - **Production is deployed first.** It owns the container registry that staging borrows — see
>   [Part 11](#part-11--deploy-staging). The first production release is therefore the only one
>   never validated in staging; from the second release onward the pipeline enforces it.
> - Steps marked **CHECKPOINT** must pass before you continue. Do not carry a failure forward:
>   almost every multi-hour problem in [10-ISSUE-LOG.md](10-ISSUE-LOG.md) was a skipped check.

---

## Contents

| Part | What | Time |
|---|---|---|
| [0](#part-0--what-you-are-building) | What you are building, and what it costs | read |
| [1](#part-1--prerequisites) | Prerequisites | 5 min |
| [2](#part-2--open-cloud-shell-and-upload-the-code) | Cloud Shell + upload the code | 5 min |
| [3](#part-3--environment-variables) | Environment variables | 5 min |
| [4](#part-4--preflight-region-and-providers) | Preflight: region and providers | 10 min |
| [5](#part-5--terraform-state-backend) | Terraform state backend | 5 min |
| [6](#part-6--application-secrets) | Application secrets | 10 min |
| [7](#part-7--deploy-production-infrastructure) | Deploy production | 25 min |
| [8](#part-8--verify-production) | Verify production | 5 min |
| [9](#part-9--create-the-first-workspace-and-superadmin) | First workspace + superadmin | 10 min |
| [10](#part-10--bind-gtminfojoycom) | Bind `gtm.infojoy.com` | 15 min |
| [11](#part-11--deploy-staging) | Deploy staging | 20 min |
| [12](#part-12--azure-devops-cicd) | Azure DevOps CI/CD | 30 min |
| [13](#part-13--monitoring-and-alerts) | Monitoring and alerts | 10 min |
| [14](#part-14--verify-backups) | Verify backups | 10 min |
| [15](#part-15--rollback-drill) | Rollback drill | 10 min |
| [16](#part-16--lock-down-admin-access) | Lock down admin access | 10 min |
| [17](#part-17--production-smoke-tests) | Production smoke tests | 10 min |
| [18](#part-18--troubleshooting-index) | Troubleshooting index | reference |

---

## Part 0 — What you are building

```
                        Internet
                           │
                    gtm.infojoy.com
                           │  HTTPS, Microsoft-managed certificate
        ┌──────────────────▼──────────────────────────────┐
        │  Container Apps environment  (VNet-integrated)  │
        │                                                 │
        │   nexus-prod-app        external ingress :8000  │
        │     FastAPI + React SPA, 1-3 replicas           │
        │     runs migrations + RLS on boot               │
        │            │                      │             │
        │            │ redis://             │             │
        │   nexus-prod-valkey     internal TCP :6379      │
        │     queue + idempotency, exactly 1 replica      │
        │            │                                    │
        │   nexus-prod-worker     no ingress              │
        │     queue consumer + scheduler, 1 replica       │
        └────────────┼────────────────────────────────────┘
                     │  private, delegated subnet — no public endpoint
          PostgreSQL Flexible Server (B_Standard_B1ms)
            RLS enforced via the least-privilege nexus_app role
            14-day PITR + geo-redundant backup + CanNotDelete lock
```

Also created: Azure Container Registry (shared with staging), Log Analytics, an action group and
restart alert, a VNet with three subnets, and a private DNS zone for Postgres.

### Cost

**These are estimates from Azure list pricing, not quotes, and not verified against your
subscription.** Check the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/)
for your region before committing. Actual bills vary with traffic, log volume and egress.

| Resource | SKU | Prod | Staging |
|---|---|---|---|
| PostgreSQL Flexible Server | B_Standard_B1ms, 32 GB | ~$25 | ~$25 |
| Backups | 14d geo-redundant / 7d local | ~$5 | ~$1 |
| Container Apps (app) | 0.5 vCPU / 1 GiB, 1-3 replicas | ~$25 | ~$18 |
| Container Apps (worker) | 0.25 vCPU / 0.5 GiB, 1 replica | ~$12 | ~$12 |
| Container Apps (valkey) | 0.25 vCPU / 0.5 GiB, 1 replica | ~$10 | ~$10 |
| Log Analytics | 30d / 7d retention | ~$8 | ~$3 |
| Container Registry | Basic (shared) | ~$5 | $0 |
| **Total** | | **~$90/mo** | **~$69/mo** |

Roughly **$160/month** for both. The two largest levers if that is too high: run staging only
during release windows (`az containerapp update --min-replicas 0`), or drop staging's Log Analytics
retention to the 7-day minimum, already set.

### What is deliberately not included

- **No WAF / Front Door** (~$35/mo). Container Apps ingress terminates TLS and Microsoft manages
  the certificate. Add Front Door when you need geo-routing, custom WAF rules or DDoS L7
  protection — none of which 10-15 users require. It is an additive change.
- **No database high availability.** ZoneRedundant HA roughly doubles the database bill to buy
  faster *recovery*. Backups and PITR already cover data *loss*, which is the different and more
  important guarantee. Flip `pg_ha_enabled = true` when an SLA justifies it.
- **No Azure Bastion** (~$140/mo). [13-DATABASE-ACCESS.md](13-DATABASE-ACCESS.md) reaches the
  private database through `az containerapp exec` at no cost.

---

## Part 1 — Prerequisites

You need:

1. **An Azure subscription** where you hold **Owner** (or Contributor + User Access Administrator).
   Contributor alone is not enough — creating the deletion locks in
   [Part 7](#part-7--deploy-production-infrastructure) requires `Microsoft.Authorization/locks/write`.
2. **DNS control over `infojoy.com`** — you will add one CNAME and one TXT record.
3. **An Azure DevOps organisation** for CI/CD ([Part 12](#part-12--azure-devops-cicd)). Free at
   [dev.azure.com](https://dev.azure.com).
4. **The application source**, as a zip you can upload.

**You do NOT need:** Docker, Terraform, Python, or the Azure CLI installed locally. Cloud Shell has
all of them.

### Confirm your access

```bash
az login
az account show --query "{subscription:name, id:id, tenant:tenantId}" -o table
```

Expected: your subscription name and id.

```bash
az role assignment list --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --query "[].roleDefinitionName" -o tsv | sort -u
```

Expected: `Owner`, or both `Contributor` and `User Access Administrator`.

> **If you see only `Contributor`:** everything works up to the deletion locks, which fail with
> `AuthorizationFailed`. Either get Owner, or set `env = "prod-nolock"`… **no** — do not do that;
> it changes every resource name. Instead ask your subscription owner to grant the role, or
> temporarily comment out `deploy/cloud/azure/protection.tf` and record that the database is
> unprotected. The lock is a real control, not a formality.

---

## Part 2 — Open Cloud Shell and upload the code

### 2.1 Open Cloud Shell

Go to [shell.azure.com](https://shell.azure.com), or click the `>_` icon in the Azure Portal.

Choose **Bash** (not PowerShell). On first use Azure offers to create a storage account for your
home directory — accept; it is a few cents a month and it is what makes your files survive a
session timeout.

**Verify the tools are present:**

```bash
terraform version && az version --query '"azure-cli"' && python3 --version
```

Expected: Terraform ≥ 1.6, an `az` version, Python 3.x. All three are preinstalled.

### 2.2 Upload the source

Click the **Upload/Download files** icon (⬆⬇) in the Cloud Shell toolbar → **Upload** → choose
`nexus-azure-deploy.zip`.

```bash
cd ~
# The archive already contains a top-level `nexus/` directory — do NOT pass `-d nexus`,
# or you get ~/nexus/nexus/ and every path below is wrong by one level.
unzip -o nexus-azure-deploy.zip
cd ~/nexus
ls -la deploy/cloud/azure/
```

Expected: `container_apps.tf`, `data.tf`, `network.tf`, `platform.tf`, `protection.tf`,
`variables.tf`, `versions.tf`, `outputs.tf`, `monitoring.tf`.

**CHECKPOINT — confirm you have the fixed version:**

```bash
grep -q 'template\[0\].container\[0\].image' deploy/cloud/azure/container_apps.tf \
  && echo "OK: image-drift fix present" || echo "MISSING — do not deploy"
grep -q 'acr_shared_name' deploy/cloud/azure/platform.tf \
  && echo "OK: shared-registry support present" || echo "MISSING — do not deploy"
test -f deploy/cloud/azure/protection.tf \
  && echo "OK: deletion locks present" || echo "MISSING — do not deploy"
```

All three must print `OK`.

> **If Cloud Shell times out** (it disconnects after ~20 minutes idle), your files persist in
> `~/nexus`. Reconnect and `cd ~/nexus`. **Environment variables do NOT persist** — re-run
> [Part 3](#part-3--environment-variables). This is issue D3 in the log and it will happen to you.

---

## Part 3 — Environment variables

Everything downstream reads these. **Re-run this whole block after any Cloud Shell reconnect.**

Save it as a file so re-running is one command:

```bash
cat > ~/nexus-env.sh <<'EOF'
# ---- Identity -------------------------------------------------------------
export AZURE_SUBSCRIPTION_ID="REPLACE_WITH_YOUR_SUBSCRIPTION_ID"

# ---- Naming ---------------------------------------------------------------
export PROJECT="nexus"
export AZURE_LOCATION="eastus2"      # verified in Part 4 — do NOT change blind

# ---- Domain ---------------------------------------------------------------
export DOMAIN="gtm.infojoy.com"
export STAGING_DOMAIN="staging-gtm.infojoy.com"

# ---- Operations -----------------------------------------------------------
export ALARM_EMAIL="REPLACE_WITH_YOUR_OPS_EMAIL"

# ---- Admin access (Part 16) -----------------------------------------------
export ADMIN_IP_1="203.199.234.154"
export ADMIN_IP_2="REPLACE_OR_LEAVE_BLANK"

# ---- Terraform state (Part 5) ---------------------------------------------
export TFSTATE_RG="nexus-tfstate-rg"
# Storage account names are GLOBALLY unique across all of Azure, 3-24 chars,
# lowercase letters and digits only. Part 5 generates one and rewrites this.
export TFSTATE_SA="REPLACE_IN_PART_5"
export TFSTATE_CONTAINER="tfstate"

# ---- Derived (do not edit) ------------------------------------------------
export PROD_RG="${PROJECT}-prod-rg"
export STAGING_RG="${PROJECT}-staging-rg"
export PROD_APP="${PROJECT}-prod-app"
export PROD_WORKER="${PROJECT}-prod-worker"
export STAGING_APP="${PROJECT}-staging-app"
export STAGING_WORKER="${PROJECT}-staging-worker"
EOF

# EDIT THE THREE 'REPLACE_' VALUES NOW:
code ~/nexus-env.sh        # Cloud Shell's built-in editor; Ctrl-S saves, Ctrl-Q quits

source ~/nexus-env.sh
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
```

**Verify:**

```bash
echo "sub=$AZURE_SUBSCRIPTION_ID"; echo "domain=$DOMAIN"; echo "rg=$PROD_RG"; echo "alarm=$ALARM_EMAIL"
```

Expected: no `REPLACE_` text except `TFSTATE_SA` (set in Part 5) and possibly `ADMIN_IP_2`.

> **`ALARM_EMAIL` matters more than it looks.** Left empty, Terraform creates an action group with
> **zero recipients**. The portal still shows an alert rule, so it reads as configured while
> notifying nobody. That is issue G7, and it is worse than having no alert at all.

---

## Part 4 — Preflight: region and providers

**Do not skip this.** Three deploys were lost to the problem it catches.

### 4.1 Verify PostgreSQL is available in your region

PostgreSQL Flexible Server provisioning is **restricted** in some regions on some subscriptions.
Azure reports this as `400 ParameterOutOfRange: The value of the 'Version' should be in: []` — an
**empty list**, which reads as "PostgreSQL 16 is not supported" and sends you off to change the
version. The version is not the problem.

A healthy region returns tens of thousands of bytes; a restricted one, a few hundred.

```bash
for r in eastus eastus2 centralus westus3 westeurope; do
  echo "$r: $(az postgres flexible-server list-skus -l $r -o json 2>/dev/null | wc -c) bytes"
done
```

Expected — note `eastus` is typically restricted, which is why the default is `eastus2`:

```
eastus: 312 bytes
eastus2: 48213 bytes
centralus: 47980 bytes
westus3: 47102 bytes
westeurope: 48001 bytes
```

**CHECKPOINT:** `$AZURE_LOCATION` must show **tens of thousands** of bytes.

```bash
BYTES=$(az postgres flexible-server list-skus -l "$AZURE_LOCATION" -o json 2>/dev/null | wc -c)
[ "$BYTES" -gt 10000 ] && echo "OK: $AZURE_LOCATION supports PostgreSQL ($BYTES bytes)" \
  || { echo "RESTRICTED: pick another region and update ~/nexus-env.sh"; }
```

> **Everything is regional and the database must sit in the same region as its delegated subnet.**
> Changing this after deploying means rebuilding the entire stack, not just the database. Get it
> right now.

### 4.2 Register resource providers

A fresh subscription has these unregistered, and the errors do not say so. Registration is
idempotent and near-instant when already done.

```bash
for ns in Microsoft.App Microsoft.DBforPostgreSQL Microsoft.OperationalInsights \
          Microsoft.ContainerRegistry Microsoft.Network Microsoft.Storage \
          Microsoft.Insights Microsoft.ManagedIdentity; do
  state=$(az provider show --namespace "$ns" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)
  if [ "$state" != "Registered" ]; then
    echo "registering $ns (currently $state)..."
    az provider register --namespace "$ns" --wait
  else
    echo "$ns already Registered"
  fi
done
```

Expected: every line ends `Registered`. First run takes 2-5 minutes.

**Verify:**

```bash
az provider list --query "[?namespace=='Microsoft.App' || namespace=='Microsoft.DBforPostgreSQL'].{ns:namespace,state:registrationState}" -o table
```

Both must read `Registered`.

**Troubleshooting**

| Symptom | Cause | Fix |
|---|---|---|
| `409 MissingSubscriptionRegistration: Microsoft.App` | provider unregistered | re-run the loop above |
| `'Version' should be in: []` | provider unregistered **or** region restricted | 4.2 then 4.1 — in that order |
| `AuthorizationFailed` on register | not Contributor+ | ask your subscription owner |

---

## Part 5 — Terraform state backend

Terraform state is the only record of what infrastructure exists. It must live in Azure Storage,
not in Cloud Shell — Cloud Shell home directories are recoverable but not durable, and CI cannot
read state it cannot reach.

### 5.1 Create a globally-unique storage account

```bash
source ~/nexus-env.sh
export TFSTATE_SA="nexustfstate$(date +%s | tail -c 7)$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "Storage account: $TFSTATE_SA  (${#TFSTATE_SA} chars — must be 3-24)"

sed -i "s|^export TFSTATE_SA=.*|export TFSTATE_SA=\"$TFSTATE_SA\"|" ~/nexus-env.sh
grep TFSTATE_SA ~/nexus-env.sh
```

Expected: a name like `nexustfstate1234567a3f9c`, under 24 characters, recorded in your env file.

### 5.2 Create the resource group, account and container

```bash
az group create --name "$TFSTATE_RG" --location "$AZURE_LOCATION" --output none
echo "resource group created"

az storage account create \
  --name "$TFSTATE_SA" --resource-group "$TFSTATE_RG" --location "$AZURE_LOCATION" \
  --sku Standard_LRS --kind StorageV2 \
  --https-only true --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --output none
echo "storage account created"
```

**State contains secrets in plaintext** — the Postgres passwords and every API key passed through
`var.secrets`. Hence no public blob access and TLS 1.2 minimum. These are not optional.

```bash
# Versioning + soft delete = the recovery story for a corrupted or truncated state file.
az storage account blob-service-properties update \
  --account-name "$TFSTATE_SA" --resource-group "$TFSTATE_RG" \
  --enable-versioning true \
  --enable-delete-retention true --delete-retention-days 30 \
  --enable-container-delete-retention true --container-delete-retention-days 30 \
  --output none
echo "versioning + 30-day soft delete enabled"

# The container. Created with the ACCOUNT KEY, not --auth-mode login: subscription Contributor
# does NOT grant blob DATA access (that needs Storage Blob Data Contributor), so the AAD path
# fails for most operators who can otherwise deploy everything else. This is issue D2.
KEY=$(az storage account keys list --account-name "$TFSTATE_SA" \
        --resource-group "$TFSTATE_RG" --query '[0].value' -o tsv)
az storage container create --name "$TFSTATE_CONTAINER" \
  --account-name "$TFSTATE_SA" --account-key "$KEY" --output none
echo "state container created"
```

### 5.3 Point Terraform at your storage account

`versions.tf` ships with the previous deployment's account name. Rewrite it:

```bash
cd ~/nexus/deploy/cloud/azure
sed -i "s|storage_account_name = \"[^\"]*\"|storage_account_name = \"$TFSTATE_SA\"|" versions.tf
sed -i "s|resource_group_name  = \"[^\"]*\"|resource_group_name  = \"$TFSTATE_RG\"|" versions.tf
grep -A4 'backend "azurerm"' versions.tf
```

Expected — and note there is deliberately **no `key`**, because the key is per-environment and
supplied at `init` time:

```hcl
  backend "azurerm" {
    resource_group_name  = "nexus-tfstate-rg"
    storage_account_name = "nexustfstate1234567a3f9c"
    container_name       = "tfstate"
  }
```

**CHECKPOINT:**

```bash
az storage container show --name "$TFSTATE_CONTAINER" \
  --account-name "$TFSTATE_SA" --account-key "$KEY" --query name -o tsv
```

Expected: `tfstate`. A `404 The specified container does not exist` here becomes a confusing
`terraform init` failure later that names neither the account nor the fix (issue D1).

---

## Part 6 — Application secrets

### 6.1 Create `deploy/.env`

```bash
cd ~/nexus
cp deploy/.env.production.example deploy/.env
```

`deploy.sh` generates `POSTGRES_PASSWORD`, `NEXUS_APP_DB_PASSWORD` and `NEXUS_SECRET_KEY`
automatically, and **refuses to pass `CHANGE_ME` through as a real credential** — that shipped
once, writing the literal string `CHANGE_ME` as the Postgres admin password and the JWT signing
key. Neither fails loudly, and the value is in the repository.

### 6.2 Set the values that matter for production

```bash
cd ~/nexus
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("deploy/.env")
env = p.read_text(encoding="utf-8")

updates = {
    "NEXUS_ENV": "prod",
    "DOMAIN": "gtm.infojoy.com",
    "ACME_EMAIL": "admin@infojoy.com",
    # Real signals only. 'demo' here makes the app REFUSE TO START in prod (by design).
    "NEXUS_SIGNAL_SOURCES": "web,rss",
    "NEXUS_DEMO_SIGNALS_ENABLED": "false",
    # Browser origin for the SPA. Wrong value = every API call blocked by CORS.
    "NEXUS_CORS_ORIGINS": "https://gtm.infojoy.com",
    "NEXUS_AUTH_RATE_LIMIT_ENABLED": "true",
    "NEXUS_METRICS_ENABLED": "true",
    "NEXUS_AUTOMATION_ENABLED": "true",
    # SSRF guard on admin-supplied DSNs. 'true' makes prod REFUSE TO START (by design).
    "NEXUS_SOURCE_DB_ALLOW_PRIVATE": "false",
}
for k, v in updates.items():
    if re.search(rf"(?m)^{k}=", env):
        env = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", env)
    else:
        env += f"\n{k}={v}"
p.write_text(env, encoding="utf-8")
print("updated:", ", ".join(updates))
PY

grep -E '^(NEXUS_ENV|DOMAIN|NEXUS_SIGNAL_SOURCES|NEXUS_CORS_ORIGINS)=' deploy/.env
```

Expected:

```
NEXUS_ENV=prod
DOMAIN=gtm.infojoy.com
NEXUS_SIGNAL_SOURCES=web,rss
NEXUS_CORS_ORIGINS=https://gtm.infojoy.com
```

> **Three settings make production refuse to start, deliberately** — each is a guard whose silent
> version would be worse than a crash:
> - `NEXUS_SECRET_KEY` left at the insecure default → anyone reading the repo can forge a token
>   for any tenant.
> - `NEXUS_SIGNAL_SOURCES` containing `demo` → fabricated signals reaching real reps.
> - `NEXUS_SOURCE_DB_ALLOW_PRIVATE=true` → the SSRF guard on admin-supplied DSNs switched off.
>
> A startup crash naming the variable costs one clear message. The alternative is a deployment
> that looks fine and is not.

### 6.3 Add API keys (optional now, required for the product to do real work)

Without these the app runs with a deterministic stub — the full workflow works, but nothing
reaches a real LLM or search provider. **Everything the agents generate is sent to real buyers,
including the stub's output**, so decide deliberately.

```bash
code deploy/.env    # add NEXUS_GROQ_API_KEY / NEXUS_ANTHROPIC_API_KEY / NEXUS_EXA_API_KEY
```

These can also be added later without a redeploy through the Control plane
(`/admin` → Provider keys), which is the better path — it avoids putting keys in Terraform state.

**CHECKPOINT — no placeholders survive:**

```bash
grep -nE '=(CHANGE_ME|changeme|TODO|TBD|REPLACE_ME)$' deploy/.env && echo "^^ FIX THESE" \
  || echo "OK: no placeholder values"
```

Only the three generated secrets may still read `CHANGE_ME`; `deploy.sh` replaces those.

---

## Part 7 — Deploy production infrastructure

### 7.1 Run it

```bash
source ~/nexus-env.sh
cd ~/nexus

ENV_NAME=prod \
ALARM_EMAIL="$ALARM_EMAIL" \
AZURE_LOCATION="$AZURE_LOCATION" \
PROJECT="$PROJECT" \
  bash deploy/cloud/deploy.sh azure "$DOMAIN" 2>&1 | tee ~/deploy-prod-$(date +%F-%H%M).log
```

**This takes 20-25 minutes.** The Postgres Flexible Server alone is 8-12. Do not interrupt it; the
log is being written to your home directory either way.

What it does, in order:

1. Reads `deploy/.env`, generates the three credentials, rejects placeholders.
2. Registers resource providers (idempotent).
3. Creates the Terraform state container if missing.
4. `terraform init` with `-backend-config="key=nexus/prod.tfstate"`.
5. Applies **only** the container registry, then builds the image **on ACR** with `az acr build`
   — which is why Cloud Shell's lack of a Docker daemon does not matter.
6. Applies everything else with that image.

Expected tail:

```
Apply complete! Resources: 24 added, 0 changed, 0 destroyed.

Done. App (default ingress): https://nexus-prod-app.<random>.eastus2.azurecontainerapps.io
>> Custom domain: Bind gtm.infojoy.com: CNAME -> nexus-prod-app...azurecontainerapps.io, then ...
```

### 7.2 Record the outputs

```bash
cd ~/nexus/deploy/cloud/azure
export PROD_URL=$(terraform output -raw app_default_url)
export PROD_FQDN=$(terraform output -raw app_fqdn)
export ACR_NAME=$(terraform output -raw acr_login_server | cut -d. -f1)

cat >> ~/nexus-env.sh <<EOF
export PROD_URL="$PROD_URL"
export PROD_FQDN="$PROD_FQDN"
export ACR_NAME="$ACR_NAME"
EOF

echo "URL:  $PROD_URL"
echo "FQDN: $PROD_FQDN"
echo "ACR:  $ACR_NAME"
```

**Write `ACR_NAME` down.** Staging and both pipelines need it.

### 7.3 Troubleshooting

**`Error: creating Registry: name already in use`**

ACR names are globally unique across all of Azure. The stack derives a suffix from your
subscription id, so this should not happen — but if it does, set a manual suffix:

```bash
cd ~/nexus/deploy/cloud/azure
terraform apply -var name_suffix=v2 -var project="$PROJECT" -var env=prod \
  -var location="$AZURE_LOCATION" -var domain="$DOMAIN"
```

---

**`Error: Name unavailable for reservation` (any resource)**

Azure **reserves some names even after deletion**. If you are rebuilding after a destroy, bump the
suffix — `name_suffix=v2`, `v3`, and so on. This is exactly what that variable exists for.

---

**`'Version' should be in: []`**

Go back to [Part 4](#part-4--preflight-region-and-providers). It is the provider registration or a
restricted region, never the PostgreSQL version.

---

**`geo-redundant backup is not supported`**

Unlikely — **verified 2026-08-28 that geo-redundant backup works on `B_Standard_B1ms` in
eastus2**, so it is not gated by the Burstable tier. What *is* tier-gated is zone-redundant HA
(`pg_ha_enabled`), which Burstable genuinely refuses. The two are easily confused because both are
"redundancy" settings on the same server; only HA is restricted.

If a create is nonetheless rejected for this in your region, set it false and record the reduced
DR posture:

```bash
cd ~/nexus/deploy/cloud/azure
echo 'pg_geo_redundant_backup = false' >> terraform.tfvars
```

Do **not** change region to chase it — everything else is regional too, and you would be rebuilding
the whole stack for one backup setting.

The setting is **create-time-only**, so leaving it on from the start is what keeps the option open:
turning it on later means building a new server and migrating the data. Without it you still have
the full PITR window on locally-redundant backups — data loss, corruption and accidental deletion
are covered; losing the entire region is not.

---

**`... is blocking by customer lock ... Lock Level: CanNotDelete` during a CREATE**

A `CanNotDelete` lock at **resource-group** scope blocks any operation ARM implements as a delete —
including the service-association link a VNet-injected PostgreSQL Flexible Server writes into its
delegated subnet. The message names a *subnet* and a *delete* lock during a database *create*, so
it points at neither the cause nor the fix.

This is why [protection.tf](../../deploy/cloud/azure/protection.tf) locks **only the database
server**, never the group. If you added a group lock by hand, remove it, then re-run:

```bash
az lock list --resource-group "$PROD_RG" -o table
az lock delete --name <lock-name> --resource-group "$PROD_RG"
az lock list --resource-group "$PROD_RG" --query "length(@)" -o tsv   # must be 0
```

---

**`AuthorizationFailed` on `azurerm_management_lock`**

You are Contributor, not Owner. Locks need `Microsoft.Authorization/locks/write`. Get the role
assignment — the alternative is an unprotected production database.

---

**Apply fails partway through**

Terraform is idempotent. Fix the cause and re-run the same command; it continues from where it
stopped. **Do not** `terraform destroy` to "start clean" — you would delete a half-built estate
and, once the locks exist, fail partway through that too.

---

## Part 8 — Verify production

**CHECKPOINT — all five must pass.**

```bash
source ~/nexus-env.sh

echo "== 1. containers =="
az containerapp list -g "$PROD_RG" --query "[].{name:name,state:properties.runningStatus}" -o table
```
Expected: `nexus-prod-app`, `nexus-prod-worker`, `nexus-prod-valkey`, all `Running`.

```bash
echo "== 2. liveness =="
curl -s -o /dev/null -w "health=%{http_code}\n" "$PROD_URL/health"
```
Expected: `health=200`.

```bash
echo "== 3. readiness (proves the database is reachable) =="
curl -s "$PROD_URL/ready"
```
Expected: `{"status":"ready","db":"up"}`. A `503` means the app is up but cannot reach Postgres —
check the app logs and the delegated subnet.

```bash
echo "== 4. the API is routed under /api =="
curl -s -o /dev/null -w "api=%{http_code}\n" "$PROD_URL/api/accounts"
```
Expected: `api=401` or `403`. **`405` or `200` means the SPA catch-all answered and the API is not
reachable at the path the frontend calls** — that is issue G3, and the product is unusable.

```bash
echo "== 5. the frontend is in the image =="
curl -s "$PROD_URL/" | grep -q 'id="root"' && echo "SPA OK" || echo "SPA MISSING"
```
Expected: `SPA OK`.

```bash
echo "== 6. database is private =="
az postgres flexible-server show -g "$PROD_RG" \
  -n "$(az postgres flexible-server list -g "$PROD_RG" --query '[0].name' -o tsv)" \
  --query "{public:network.publicNetworkAccess, geoBackup:backup.geoRedundantBackup, retention:backup.backupRetentionDays}" -o table
```
Expected: `Disabled`, `Enabled`, `14`.

```bash
echo "== 7. deletion locks =="
az lock list -g "$PROD_RG" --query "[].{name:name,level:level}" -o table
```
Expected: two `CanNotDelete` locks.

**If any check fails, read the logs before changing anything:**

```bash
az containerapp logs show -n "$PROD_APP" -g "$PROD_RG" --tail 100
```

### 8.1 Set the public base URL

This could not be set earlier — it needs the ingress hostname, which only exists after the apply.

```bash
source ~/gtm-env.sh
echo "NEXUS_APP_BASE_URL=$PROD_URL" >> ~/nexus/deploy/.env
for APPNAME in "$PROD_APP" "$PROD_WORKER"; do
  az containerapp update -n "$APPNAME" -g "$PROD_RG" --output none \
    --set-env-vars "NEXUS_APP_BASE_URL=$PROD_URL"
done
```

**Skipping this is silent.** Every link in transactional email is built from it, and an empty value
yields a *relative* path — a password-reset mail arrives reading `Reset it here:
/reset-password?email=...&token=...`, which cannot be clicked. The mail sends, the endpoint returns
202, nothing logs an error, and the only symptom is a customer who cannot reset their password.

Both containers get it: the worker builds links in digests and alerts the same way.

**Update it again when you bind a custom domain** ([Part 10](#part-10--bind-gtminfojoycom)), or
every reset link keeps pointing at the raw `*.azurecontainerapps.io` hostname — which reads as a
phishing link to whoever receives it.

---

## Part 9 — Create the first workspace and superadmin

A fresh deployment has an **empty `users` and `tenants` table**, and **the UI cannot create the
first one**. `LoginPage.tsx` always calls the OTP registration flow, so without SMTP configured the
form waits forever for a code that is never sent — with no error explaining why (issue G5).

### 9.1 Create the first workspace through the API

`/api/auth/signup` is single-step and needs no email. It is closed only when
`NEXUS_OTP_REGISTRATION_ENABLED=true`, which defaults to false.

Use the API rather than writing rows directly: it runs the real registration path, so the Tenant,
User, Workspace and owner Membership are all created consistently — this app's `User` is **global**
and joined to a tenant through a `Membership`, so hand-built inserts miss a row and produce an
account that exists but belongs to nothing.

```bash
source ~/nexus-env.sh

curl -s -X POST "$PROD_URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name": "Infojoy",
  "company_slug": "infojoy",
  "email": "you@infojoy.com",
  "full_name": "Your Name",
  "password": "REPLACE-WITH-A-STRONG-PASSWORD"
}' | python3 -m json.tool
```

Expected: HTTP 201 and a JSON body containing `access_token`. The first user of a workspace is
`owner`.

| Failure | Meaning |
|---|---|
| `403 Email verification required` | `NEXUS_OTP_REGISTRATION_ENABLED=true` — set it false and redeploy |
| `409 Company slug already taken` | pick a different slug |
| `409 Email already registered` | the user exists; just log in |
| `422` | slug must match `^[a-z0-9][a-z0-9-]{1,79}$`; password ≥ 8 characters |

**Verify login works** (login is a normal flow and needs no email — only *registration* is gated):

```bash
curl -s -X POST "$PROD_URL/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"you@infojoy.com","password":"REPLACE-WITH-A-STRONG-PASSWORD"}' \
  | head -c 120
```

Expected: a body containing `access_token`.

### 9.2 Grant platform superadmin

Platform admin is **completely separate from tenant RBAC**. No workspace role — not even `owner` —
grants access to `/admin` (issue G6).

The environment allowlist is the bootstrap mechanism and deliberately carries **full** permissions;
it exists to solve "nobody can reach the console yet", and narrowing it would reintroduce the
lockout it prevents.

```bash
az containerapp update -n "$PROD_APP" -g "$PROD_RG" \
  --set-env-vars "NEXUS_PLATFORM_ADMIN_EMAILS=you@infojoy.com" --output none

echo "waiting for the new revision..."
until [ "$(az containerapp show -n "$PROD_APP" -g "$PROD_RG" --query properties.runningStatus -o tsv)" = "Running" ]; do sleep 10; done
curl -s "$PROD_URL/ready"
```

**Verify** by opening `$PROD_URL/admin` in a browser, logged in as that address.

**Now persist it to `deploy/.env`.** The `az containerapp update` above sets the variable on the
*running* revision only. The next `terraform apply` rebuilds the container's environment from
`var.secrets`, which is read from `deploy/.env` — so without this line, a routine infrastructure
change silently removes your own platform access:

```bash
echo 'NEXUS_PLATFORM_ADMIN_EMAILS=you@infojoy.com' >> ~/nexus/deploy/.env
grep NEXUS_PLATFORM_ADMIN_EMAILS ~/nexus/deploy/.env
```

Once you can reach `/admin`, grant further admins through the console — which writes the
`platform_admins` table with per-permission grants — rather than by extending this variable. The
env allowlist is a bootstrap that carries full power; it is not the access model.

---

## Part 10 — Bind `gtm.infojoy.com`

Until now the app serves on its `*.azurecontainerapps.io` hostname. Binding is **additive** — no
rebuild.

### 10.1 Get the two DNS values

```bash
source ~/nexus-env.sh
echo "CNAME target : $PROD_FQDN"
echo "TXT value    : $(az containerapp show -n "$PROD_APP" -g "$PROD_RG" \
                        --query properties.customDomainVerificationId -o tsv)"
```

### 10.2 Add the records at your DNS provider

| Type | Name | Value | TTL |
|---|---|---|---|
| `CNAME` | `gtm` | `<CNAME target above>` | 300 |
| `TXT` | `asuid.gtm` | `<TXT value above>` | 300 |

The `asuid.` TXT record proves you control the name; Azure refuses to issue a certificate without
it.

> **CNAME the app FQDN, never a revision hostname.** A revision hostname
> (`nexus-prod-app--3acvd46...`) is correct only until the next deploy, after which it serves
> Azure's own "Container App - Unavailable" HTML — which looks like your application is broken
> rather than like a stale DNS record.

### 10.3 Wait for propagation, then verify

```bash
until [ -n "$(dig +short CNAME gtm.infojoy.com)" ]; do echo "waiting for CNAME..."; sleep 30; done
dig +short CNAME gtm.infojoy.com
dig +short TXT asuid.gtm.infojoy.com
```

Both must return values before continuing. **Binding before DNS resolves fails**, and the failure
message does not mention DNS.

### 10.4 Bind and issue the certificate

```bash
ENVID=$(az containerapp show -n "$PROD_APP" -g "$PROD_RG" --query properties.managedEnvironmentId -o tsv)

az containerapp hostname add \
  --hostname "$DOMAIN" -n "$PROD_APP" -g "$PROD_RG" --output none
echo "hostname added"

az containerapp hostname bind \
  --hostname "$DOMAIN" -n "$PROD_APP" -g "$PROD_RG" \
  --environment "$ENVID" --validation-method CNAME --output none
echo "bound — certificate issuing (2-5 minutes)"
```

**Verify:**

```bash
until curl -sf -o /dev/null "https://$DOMAIN/health"; do echo "waiting for certificate..."; sleep 20; done
curl -s "https://$DOMAIN/ready"
curl -sI "https://$DOMAIN/" | grep -i '^HTTP'
```

Expected: `{"status":"ready","db":"up"}` and `HTTP/2 200`.

### 10.5 Fix CORS now that the real origin exists

`NEXUS_CORS_ORIGINS` was set in Part 6, but confirm the running container has it — if the browser
origin does not match, every API call from the SPA is blocked and the product appears completely
broken while `curl` works perfectly.

```bash
az containerapp show -n "$PROD_APP" -g "$PROD_RG" \
  --query "properties.template.containers[0].env[?name=='NEXUS_CORS_ORIGINS']" -o table
```

Expected: `https://gtm.infojoy.com`. If it is wrong:

```bash
az containerapp update -n "$PROD_APP" -g "$PROD_RG" \
  --set-env-vars "NEXUS_CORS_ORIGINS=https://gtm.infojoy.com" --output none
```

### 10.6 Repoint everything that hardcoded the old hostname

Three values were set to the ACA ingress FQDN before the domain existed. Binding the domain does
**not** update them, and each fails quietly:

```bash
source ~/gtm-env.sh

# 1. Transactional email links — otherwise every password-reset and OTP mail sends
#    customers to a raw *.azurecontainerapps.io URL that reads as a phishing link.
sed -i "s|^NEXUS_APP_BASE_URL=.*|NEXUS_APP_BASE_URL=https://$DOMAIN|" ~/nexus/deploy/.env
for APPNAME in "$PROD_APP" "$PROD_WORKER"; do
  az containerapp update -n "$APPNAME" -g "$PROD_RG" --output none \
    --set-env-vars "NEXUS_APP_BASE_URL=https://$DOMAIN"
done

# 2. CORS, for completeness. The SPA is served same-origin by FastAPI so this is not
#    load-bearing today, but it becomes so the moment anything calls the API cross-origin.
az containerapp update -n "$PROD_APP" -g "$PROD_RG" --output none \
  --set-env-vars "NEXUS_CORS_ORIGINS=https://$DOMAIN"

grep -E '^(NEXUS_APP_BASE_URL|NEXUS_CORS_ORIGINS)=' ~/nexus/deploy/.env
```

**3. The pipeline's `APP_URL` variable** — in Azure DevOps, Library → `gtm-deploy` → set `APP_URL`
to `https://gtm.infojoy.com`. Until you do, the release smoke test gates on a hostname customers
never visit, so a broken custom-domain binding would pass CD.

Verify a link is now absolute — note the 60-second per-account cooldown on reset emails:

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "https://$DOMAIN/api/auth/forgot-password" \
  -H 'Content-Type: application/json' -d '{"email":"you@infojoy.com"}'
```

Then read the inbox. The link must begin `https://gtm.infojoy.com/reset-password?...`. A 202 alone
proves nothing — the mail path never raises, so failures are logged and swallowed.

**Now open `https://gtm.infojoy.com` in a browser and log in.** This is the first end-to-end proof.

---

## Part 11 — Deploy staging

Staging borrows production's registry so a release can be promoted as **the same image digest**.
Two registries would make promotion a copy, and a copy is a different artifact.

```bash
source ~/nexus-env.sh
cd ~/nexus

ENV_NAME=staging \
ALARM_EMAIL="$ALARM_EMAIL" \
AZURE_LOCATION="$AZURE_LOCATION" \
PROJECT="$PROJECT" \
ACR_SHARED_NAME="$ACR_NAME" \
ACR_SHARED_RG="$PROD_RG" \
  bash deploy/cloud/deploy.sh azure "$STAGING_DOMAIN" 2>&1 | tee ~/deploy-staging-$(date +%F-%H%M).log
```

The state key is `nexus/staging.tfstate` — a **different file** from production's. That separation
is what stops one environment's apply proposing the destruction of the other.

**CHECKPOINT:**

```bash
cd ~/nexus/deploy/cloud/azure
export STAGING_URL=$(terraform output -raw app_default_url)
echo "export STAGING_URL=\"$STAGING_URL\"" >> ~/nexus-env.sh

curl -s "$STAGING_URL/ready"
az containerapp list -g "$STAGING_RG" --query "[].{name:name,state:properties.runningStatus}" -o table

echo "== both registries must be the SAME =="
az containerapp show -n "$STAGING_APP" -g "$STAGING_RG" --query "properties.template.containers[0].image" -o tsv
az containerapp show -n "$PROD_APP"    -g "$PROD_RG"    --query "properties.template.containers[0].image" -o tsv
```

Expected: `{"status":"ready","db":"up"}`, three Running containers, and **both image lines starting
with the same `<acr>.azurecr.io/` host**. Different hosts mean `ACR_SHARED_NAME` did not take
effect and the release pipeline cannot promote correctly.

**Verify the environments are genuinely isolated** — different databases, no shared secrets:

```bash
az postgres flexible-server list -g "$PROD_RG"    --query "[].name" -o tsv
az postgres flexible-server list -g "$STAGING_RG" --query "[].name" -o tsv
```

Expected: two different server names. If they match, stop — staging is writing to production.

**Staging has no deletion locks, deliberately.** It exists to be rebuilt:

```bash
ENV_NAME=staging PROJECT="$PROJECT" AZURE_LOCATION="$AZURE_LOCATION" \
  bash deploy/cloud/deploy.sh azure "$STAGING_DOMAIN" destroy
```

---

## Part 12 — Azure DevOps CI/CD

### 12.1 Create the project and connect GitHub

1. Go to [dev.azure.com](https://dev.azure.com) → **New project** → name it `nexus-gtm` → **Private**.
2. **Project settings** → **Service connections** → **New** → **GitHub** → authorize → name it
   `github-nexus`.

### 12.2 Create the Azure service connection (workload identity — no stored secret)

**Project settings** → **Service connections** → **New** → **Azure Resource Manager** →
**Workload Identity federation (automatic)** → your subscription → name it exactly
**`nexus-azure`**.

> Choose federation, not a service principal secret. Federated credentials are short-lived and
> issued per-run; a stored secret is a long-lived key sitting in a settings page that must be
> rotated and never is.

### 12.3 Scope the pipeline's permissions

By default the connection is Contributor on the whole subscription — enough to delete production.
Narrow it to the two resource groups it actually deploys to:

```bash
source ~/nexus-env.sh
SP_ID=$(az ad sp list --display-name "nexus-gtm-*" --query "[0].id" -o tsv)   # confirm in the portal
SUB="/subscriptions/$AZURE_SUBSCRIPTION_ID"

az role assignment delete --assignee "$SP_ID" --role Contributor --scope "$SUB" 2>/dev/null || true

for RG in "$PROD_RG" "$STAGING_RG"; do
  az role assignment create --assignee "$SP_ID" --role Contributor \
    --scope "$SUB/resourceGroups/$RG" --output none
done
az role assignment create --assignee "$SP_ID" --role AcrPush \
  --scope "$(az acr show -n "$ACR_NAME" --query id -o tsv)" --output none

az role assignment list --assignee "$SP_ID" --query "[].{role:roleDefinitionName,scope:scope}" -o table
```

Expected: Contributor on the two resource groups and AcrPush on the registry — **and nothing at
subscription scope**. Note the pipeline identity holds no lock-management permission, so **CI
cannot delete the production database** even if compromised.

### 12.4 Set pipeline variables

**Pipelines** → **Library** → **+ Variable group** → name `nexus-deploy`:

```bash
source ~/nexus-env.sh
cat <<EOF

AZURE_SUBSCRIPTION      nexus-azure
ACR_NAME                $ACR_NAME

RESOURCE_GROUP          $PROD_RG
APP_NAME                $PROD_APP
WORKER_NAME             $PROD_WORKER
APP_URL                 https://$DOMAIN

STAGING_RESOURCE_GROUP  $STAGING_RG
STAGING_APP_NAME        $STAGING_APP
STAGING_WORKER_NAME     $STAGING_WORKER
STAGING_APP_URL         $STAGING_URL

EOF
```

Copy those pairs into the variable group, then **Pipeline permissions** → allow both pipelines.

### 12.5 Create the environments and the approval gate

**Pipelines** → **Environments** → **New environment**:

- `staging` — no approvers. Staging is where the artifact *earns* the right to be promoted; a human
  gate there only adds latency to a decision nobody can make without the result.
- `production` — **Approvals and checks** → **Approvals** → add yourself → **Save**.

**This approval is the production gate.** Without it CD deploys straight through.

### 12.6 Create the pipelines

**Pipelines** → **New pipeline** → **GitHub** → your repo → **Existing Azure Pipelines YAML file**:

- `/azure-pipelines-ci.yml` → save as **nexus-CI**
- `/azure-pipelines-cd.yml` → save as **nexus-CD**

Link the `nexus-deploy` variable group to both (**Edit** → **Variables** → **Variable groups**).

### 12.7 Prove it works

> **The uploaded zip has no `.git` directory.** It exists to build infrastructure from Cloud Shell,
> not to be the source of truth. CI/CD builds from your **GitHub repository**, so these commits
> must come from your local clone — pushing from Cloud Shell would mean re-authenticating GitHub
> there and creating a second working copy that immediately diverges.
>
> Run this **on your own machine**, in your existing clone, after copying across the changed files
> (`deploy/`, `azure-pipelines-*.yml`, `docs/deployment/`):

```bash
git add -A
git commit -m "chore(deploy): staging environment, shared registry, deletion locks"
git push origin master
```

Watch **nexus-CI**. Expected stages: `Changes` → `Fast` → `Test` → `Package`. The `Package` stage
builds on ACR and Trivy-scans the image.

**Verify the image landed:**

```bash
az acr repository show-tags -n "$ACR_NAME" --repository nexus-gtm -o table | head
```

Expected: a tag equal to your commit SHA.

Then cut a release:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

**nexus-CD** runs: `Verify` → `DeployStaging` → `ValidateStaging` → **approval** →
`DeployProduction`. Approve when staging validation is green.

> **CD never builds.** It resolves the tag to a commit SHA and refuses to deploy if that image is
> not already in ACR. A deploy-time build would ship an image nothing scanned, and the whole point
> of the staging gate is that production runs the byte-identical artifact staging validated.

---

## Part 13 — Monitoring and alerts

Terraform already created an action group and a container-restart alert. Confirm the action group
actually has a recipient — an empty one reads as configured and notifies nobody:

```bash
source ~/nexus-env.sh
az monitor action-group show -g "$PROD_RG" -n "${PROJECT}-prod-alerts" \
  --query "emailReceivers[].{name:name,email:emailAddress}" -o table
```

Expected: your `ALARM_EMAIL`. **If this is empty, alerting is decorative.** Fix:

```bash
az monitor action-group update -g "$PROD_RG" -n "${PROJECT}-prod-alerts" \
  --add-action email ops "$ALARM_EMAIL"
```

### Add the alerts that matter beyond restarts

```bash
AG=$(az monitor action-group show -g "$PROD_RG" -n "${PROJECT}-prod-alerts" --query id -o tsv)
PG=$(az postgres flexible-server list -g "$PROD_RG" --query '[0].id' -o tsv)

# Database CPU — the upsize trigger.
az monitor metrics alert create -n "${PROJECT}-prod-db-cpu" -g "$PROD_RG" --scopes "$PG" \
  --condition "avg cpu_percent > 80" --window-size 15m --evaluation-frequency 5m \
  --severity 2 --action "$AG" --description "Postgres CPU above 80% for 15 minutes" --output none

# Connection exhaustion — the binding constraint on this architecture, and it bites during a
# rollout rather than in steady state.
az monitor metrics alert create -n "${PROJECT}-prod-db-conns" -g "$PROD_RG" --scopes "$PG" \
  --condition "avg active_connections > 40" --window-size 5m --evaluation-frequency 1m \
  --severity 1 --action "$AG" --description "Postgres connections above 40 of ~50" --output none

# Storage — a full disk stops writes entirely.
az monitor metrics alert create -n "${PROJECT}-prod-db-storage" -g "$PROD_RG" --scopes "$PG" \
  --condition "avg storage_percent > 85" --window-size 30m --evaluation-frequency 15m \
  --severity 2 --action "$AG" --description "Postgres storage above 85%" --output none

az monitor metrics alert list -g "$PROD_RG" --query "[].{name:name,enabled:enabled}" -o table
```

### Availability test

The alerts above watch infrastructure. None of them notices "the site returns 500 to every user".

```bash
WS=$(az monitor log-analytics workspace show -g "$PROD_RG" -n "${PROJECT}-prod-logs" --query id -o tsv)
az monitor app-insights component create --app "${PROJECT}-prod-ai" -g "$PROD_RG" \
  --location "$AZURE_LOCATION" --workspace "$WS" --output none
echo "Application Insights created — add a standard availability test against https://$DOMAIN/ready"
echo "Portal: Application Insights > ${PROJECT}-prod-ai > Availability > Add standard test"
```

Point it at `/ready`, not `/health` — readiness includes database reachability, so it catches the
failure mode that matters.

---

## Part 14 — Verify backups

**A backup is only real once a restore has been tested.** Confirm configuration, then actually
restore.

```bash
source ~/nexus-env.sh
PG=$(az postgres flexible-server list -g "$PROD_RG" --query '[0].name' -o tsv)

az postgres flexible-server show -g "$PROD_RG" -n "$PG" \
  --query "{retention:backup.backupRetentionDays, geo:backup.geoRedundantBackup, earliest:backup.earliestRestoreDate}" -o table
```

Expected: `14`, `Enabled`, and an `earliest` timestamp. **No `earliest` value means no restore
point exists yet** — wait; the first base backup takes a few hours after creation.

### Restore drill (safe — creates a NEW server, touches nothing live)

```bash
RESTORE_POINT=$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)
az postgres flexible-server restore \
  --resource-group "$PROD_RG" --name "${PG}-restoretest" \
  --source-server "$PG" --restore-time "$RESTORE_POINT" --output none
echo "restore started (10-20 minutes)"

az postgres flexible-server show -g "$PROD_RG" -n "${PG}-restoretest" --query state -o tsv
```

Expected eventually: `Ready`.

**DELETE IT when the drill is done** — it is a full-price second database:

```bash
az postgres flexible-server delete -g "$PROD_RG" -n "${PG}-restoretest" --yes
az postgres flexible-server list -g "$PROD_RG" --query "[].name" -o tsv   # only the original
```

Record in [10-ISSUE-LOG.md](10-ISSUE-LOG.md): date tested, restore point, time taken. **RPO** is
your PITR window (seconds to minutes of data). **RTO** is what you just measured — typically 15-25
minutes plus DNS.

> **What is NOT backed up: the Valkey queue.** It runs with no persistence, so a restart loses
> in-flight jobs. Periodic sweeps re-enqueue themselves and handler failures are dead-lettered to
> Postgres; what is genuinely lost is one-shot work in flight — a campaign send, an orchestration
> run. This is a deliberate trade documented in `container_apps.tf`.

---

## Part 15 — Rollback drill

Practise this before you need it.

```bash
source ~/nexus-env.sh
az containerapp revision list -n "$PROD_APP" -g "$PROD_RG" \
  --query "[].{rev:name,active:properties.active,health:properties.healthState,image:properties.template.containers[0].image}" -o table
```

### Roll back to the previous image

```bash
PREV=$(az containerapp revision list -n "$PROD_APP" -g "$PROD_RG" \
  --query "sort_by([?properties.healthState=='Healthy'], &properties.createdTime)[-2].properties.template.containers[0].image" -o tsv)
echo "previous image: $PREV"

az containerapp update -n "$PROD_APP"    -g "$PROD_RG" --image "$PREV" --output none
az containerapp update -n "$PROD_WORKER" -g "$PROD_RG" --image "$PREV" --output none

curl -s "https://$DOMAIN/ready"
```

CD does this automatically when a production deploy fails — see the `Rollback` stage.

> **A rollback does not revert database migrations.** Migrations in this repo are additive-only, so
> the previous image runs correctly against the newer schema. That is a property of the codebase,
> not of Azure — verify it holds for the specific release before relying on it.

---

## Part 16 — Lock down admin access

The Control plane (`/admin`) grants power over pricing, provider credentials and other workspaces.
Restrict it to your two addresses. This is enforced **in the application**, on top of platform-admin
authentication — a stolen admin token is worth far less if it must also arrive from the right
network.

**The allowlist accepts at most two entries.** Use a CIDR range for an office network.

1. Open `https://gtm.infojoy.com/admin` → **Runtime settings**.
2. Set `admin_ip_allowlist` to:

```bash
source ~/nexus-env.sh
if [ -n "$ADMIN_IP_2" ] && [ "$ADMIN_IP_2" != "REPLACE_OR_LEAVE_BLANK" ]; then
  echo "$ADMIN_IP_1,$ADMIN_IP_2"
else
  echo "$ADMIN_IP_1"
fi
```

3. Save, then **verify from an allowed address** that `/admin` still loads.

> **Three properties stop this becoming a lockout, and all three are deliberate:** an empty value
> means open (default-closed would lock every existing deployment out on upgrade); at most two
> entries; and a malformed list is ignored rather than enforced, because the only place to fix a bad
> allowlist is the panel it would have closed.

**Behind Container Apps ingress the observed address is `X-Forwarded-For`.** If a legitimate request
is refused, the error names the address actually seen — use that value, not the one you expected.

### Enable MFA on the superadmin account

`/settings/security` → **Enable MFA**. Store the recovery codes somewhere other than this
subscription. Login is unchanged for anyone who has not confirmed a factor, so this affects only
accounts that opt in.

---

## Part 17 — Production smoke tests

Run all of these against `https://gtm.infojoy.com` before declaring the deployment live.

```bash
source ~/nexus-env.sh
BASE="https://$DOMAIN"
pass=0; fail=0
check() { # <name> <expected-code> <path>
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$3")
  if [ "$code" = "$2" ]; then echo "PASS  $1 ($code)"; pass=$((pass+1));
  else echo "FAIL  $1 — expected $2, got $code"; fail=$((fail+1)); fi
}

check "TLS + liveness"          200 /health
check "readiness (db reachable)" 200 /ready
check "SPA served"              200 /
check "API mounted + auth"      401 /api/accounts
check "metrics exposed"         200 /metrics
check "OpenAPI docs"            200 /docs

echo "---"
curl -s "$BASE/" | grep -q 'id="root"' && { echo "PASS  SPA shell"; pass=$((pass+1)); } || { echo "FAIL  SPA shell"; fail=$((fail+1)); }
curl -sI "$BASE/" | grep -qi 'strict-transport-security' && echo "PASS  HSTS header" || echo "WARN  no HSTS header"

echo "---"; echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] && echo "SMOKE TESTS PASSED" || echo "SMOKE TESTS FAILED — do not announce this deployment"
```

**Then verify by hand, because none of the above proves the product works:**

- [ ] Log in at `https://gtm.infojoy.com` with the Part 9 account
- [ ] The dashboard renders (not a blank page — a blank page with a working API is a CORS failure)
- [ ] `/admin` loads from an allowed IP
- [ ] `/admin` is **refused** from a different network (phone hotspot)
- [ ] Create an account record; it persists after a refresh
- [ ] Worker is consuming: `az containerapp logs show -n "$PROD_WORKER" -g "$PROD_RG" --tail 50`
- [ ] Staging is a **different** database (Part 11 check)
- [ ] Deletion locks present: `az lock list -g "$PROD_RG" -o table`

---

## Part 18 — Troubleshooting index

| Symptom | Likely cause | Go to |
|---|---|---|
| `'Version' should be in: []` | provider unregistered, or restricted region | [Part 4](#part-4--preflight-region-and-providers) |
| `409 MissingSubscriptionRegistration` | provider unregistered | [Part 4](#part-4--preflight-region-and-providers) |
| `404 The specified container does not exist` on init | state container missing | [Part 5](#part-5--terraform-state-backend) |
| `AuthorizationPermissionMismatch` on the state container | Contributor ≠ blob data access | use the account key ([5.2](#52--create-the-resource-group-account-and-container)) |
| `name already in use` / `Name unavailable for reservation` | globally-unique name taken or reserved | `-var name_suffix=v2` ([7.3](#73--troubleshooting)) |
| `AuthorizationFailed` on a lock | Contributor, not Owner | [Part 1](#part-1--prerequisites) |
| `405` or `200` on `/api/...` | SPA catch-all answering; API not routed | [Part 8](#part-8--verify-production) check 4 |
| `/ready` returns 503 | app up, database unreachable | app logs; check the delegated subnet |
| Blank page, but `curl` works | CORS origin mismatch | [10.5](#105--fix-cors-now-that-the-real-origin-exists) |
| `Container App - Unavailable` HTML | DNS points at a **revision** hostname | [10.2](#102--add-the-records-at-your-dns-provider) |
| Certificate never issues | `asuid.` TXT missing or not propagated | [10.3](#103--wait-for-propagation-then-verify) |
| Alerts never arrive | action group has no receivers | [Part 13](#part-13--monitoring-and-alerts) |
| 500s during a deploy | Postgres connections exhausted | connection budget in `variables.tf` |
| `pg_dump: not found` | no client tools in the image | [13-DATABASE-ACCESS.md](13-DATABASE-ACCESS.md) Method 2 |
| Can't reach the DB from your laptop | private by design, no public endpoint | [13-DATABASE-ACCESS.md](13-DATABASE-ACCESS.md) |
| CD says "No image ... in ACR" | CI never built that commit | run CI on `master` first |
| Terraform wants to change `image` | should be ignored now | confirm the [Part 2](#22--upload-the-source) checkpoint |
| Env vars empty after a break | Cloud Shell reconnect | `source ~/nexus-env.sh` |

**Always start here:**

```bash
az containerapp logs show -n "$PROD_APP" -g "$PROD_RG" --tail 200
az containerapp logs show -n "$PROD_WORKER" -g "$PROD_RG" --tail 200
az containerapp revision list -n "$PROD_APP" -g "$PROD_RG" -o table
```

**Related:** [10-ISSUE-LOG.md](10-ISSUE-LOG.md) — 25 real failures with symptom, cause and guard ·
[08-TROUBLESHOOTING.md](08-TROUBLESHOOTING.md) · [13-DATABASE-ACCESS.md](13-DATABASE-ACCESS.md) ·
[05-BACKUP-RESTORE.md](05-BACKUP-RESTORE.md) · [11-DESTROY-REBUILD.md](11-DESTROY-REBUILD.md)

---

## Record what happened

Append to [10-ISSUE-LOG.md](10-ISSUE-LOG.md) for every problem you hit: symptom, the **exact** error
text, real cause, the command that fixed it, and how to prevent it. In 14 of the 25 recorded issues
the error message named the wrong thing — that log is the reason the next deployment is faster.
