# 01 — Launch the infrastructure in one pass

Target: from an empty subscription to a live app with **no errors**. Roughly 45 minutes, most of
it waiting on Postgres and the container build.

Everything below assumes **Azure Cloud Shell** (the `>_` icon in the portal). It has `terraform`
and `az` preinstalled and authenticated, and — importantly — **no Docker daemon**, which is why
the image is built server-side with `az acr build`.

---

## Step 0 — Preflight: verify the region BEFORE anything else

This is the single most expensive mistake available. Everything is regional, and the Postgres
server must live in the same region as its delegated subnet, so a wrong region means rebuilding
the entire stack, not just the database.

```bash
for r in eastus2 centralus westus3 eastus; do
  echo "$r: $(az postgres flexible-server list-skus -l $r -o json 2>/dev/null | wc -c) bytes"
done
```

A healthy region returns **tens of thousands of bytes**. A restricted one returns a few thousand
and contains `"reason": "Provisioning is restricted in this region"`.

> **Measured 2026-08-05:** `eastus` returned 7,836 bytes (restricted), `eastus2` 84,390 (healthy).
> Azure surfaces the restriction at create time as
> `400 ParameterOutOfRange: The value of the 'Version' should be in: []` — an **empty list** that
> reads as "PostgreSQL 16 is unsupported here". It is not the version. Three deploys were lost to
> that message before anyone checked `list-skus`.

Pick a region that returns a large catalog and use it for `location` throughout.

## Step 1 — Terraform state (once per subscription, by hand)

State must live somewhere Terraform does not manage. If it lived inside the stack, a
`terraform destroy` would delete the record of what exists.

```bash
az group create --name nexus-tfstate-rg --location eastus2 \
  --tags project=nexus env=shared managed-by=manual purpose=terraform-state
```

Storage account names are globally unique, 3–24 chars, lowercase alphanumeric only:

```bash
export SA=nexustfstate$RANDOM$RANDOM
az storage account check-name --name "$SA" --query nameAvailable
az storage account create --name "$SA" --resource-group nexus-tfstate-rg --location eastus2 \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
  --allow-blob-public-access false --https-only true
```

**Versioning and soft delete are the point of this step** — they turn "someone corrupted the state
file" from an outage into a restore:

```bash
az storage account blob-service-properties update --account-name "$SA" \
  --resource-group nexus-tfstate-rg --enable-versioning true \
  --enable-delete-retention true --delete-retention-days 30 \
  --enable-container-delete-retention true --container-delete-retention-days 30
```

```bash
az storage container create --name tfstate --account-name "$SA" \
  --account-key "$(az storage account keys list --account-name "$SA" \
    --resource-group nexus-tfstate-rg --query '[0].value' -o tsv)"
```

> **Use the account key, not `--auth-mode login`.** Subscription *Contributor* does **not** grant
> blob **data** access — that needs `Storage Blob Data Contributor` separately. The AAD path fails
> for most operators who can otherwise deploy everything else.

> **Do not skip the container.** Without it `terraform init` fails with a bare
> `404 The specified container does not exist`, which names neither the account nor the fix.
> `deploy/cloud/deploy.sh` now creates it automatically.

Point the backend at it in `deploy/cloud/azure/versions.tf`:

```hcl
backend "azurerm" {
  resource_group_name  = "nexus-tfstate-rg"
  storage_account_name = "<your SA>"
  container_name       = "tfstate"
  key                  = "nexus/prod.tfstate"
}
```

## Step 2 — Resource providers

A fresh subscription has these unregistered, and only one of the resulting errors says so.

```bash
for ns in Microsoft.App Microsoft.DBforPostgreSQL Microsoft.Cache \
          Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.Network; do
  az provider register --namespace $ns --wait
done
az provider list --query "[?namespace=='Microsoft.App'||namespace=='Microsoft.DBforPostgreSQL'].{ns:namespace,state:registrationState}" -o table
```

All must read `Registered`. `deploy.sh` does this automatically; it is listed here because a
manual `terraform apply` does not.

## Step 3 — Secrets

```bash
cd ~/nexus/deploy && cp .env.production.example .env
```

Generate the three credentials **once** and write them in. Do not leave them as `CHANGE_ME`, and do
not let them regenerate on every run — a changing `NEXUS_SECRET_KEY` invalidates every JWT and logs
out every user.

```bash
cd ~/nexus/deploy && python3 - <<'PY'
import pathlib, re, secrets
p = pathlib.Path(".env"); lines = p.read_text().splitlines(); out = []
targets = {"POSTGRES_PASSWORD", "NEXUS_APP_DB_PASSWORD", "NEXUS_SECRET_KEY"}
for ln in lines:
    m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", ln)
    if m and m.group(1) in targets and len(m.group(2)) < 16:
        out.append(f"{m.group(1)}={secrets.token_hex(24)}"); print("generated", m.group(1)); continue
    out.append(ln)
p.write_text("\n".join(out) + "\n")
PY
```

Set these too — both matter and both are easy to forget:

```
NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com   # superadmin bootstrap; empty = nobody
NEXUS_OTP_REGISTRATION_ENABLED=false          # see 03-SEED-USERS.md
```

Add provider API keys (all optional; each is inert when unset). Pools are comma-separated and the
provider rotates on a 429:

```
NEXUS_GROQ_API_KEY=...        NEXUS_GROQ_API_KEYS=k1,k2,k3
NEXUS_EXA_API_KEY=...         NEXUS_EXA_API_KEYS=k1,k2,k3
NEXUS_APIFY_API_KEY=...       NEXUS_APIFY_API_KEYS=k1,k2,k3
NEXUS_ANTHROPIC_API_KEY=...   # single only — no rotation pool exists
```

**Back it up off Cloud Shell immediately.** This file is the only copy of your database credentials
and every API key, and Cloud Shell home directories do get wiped:

```bash
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M)
```

Then download it via the Cloud Shell **⇅ → Download** button as well.

## Step 4 — One command

```bash
ALARM_EMAIL=ops@example.com AZURE_LOCATION=eastus2 deploy/cloud/deploy.sh azure app.example.com
```

That registers providers, creates the state container, builds the image on ACR, and applies in two
phases. The domain argument provisions nothing — it only feeds an output string — so a placeholder
is fine until you have a real domain.

### Or manually, in four steps

```bash
source ~/clouddrive/nexus-env.sh          # see 07-OPERATIONS.md for this helper
cd ~/nexus/deploy/cloud/azure
terraform init && terraform validate
export SFX=v1        # bump on every rebuild-from-scratch — see the warning below
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -target=azurerm_container_registry.main
export REG="$(terraform output -raw acr_login_server)"
cd ~/nexus && export TAG="$(date +%Y%m%d-%H%M)"
az acr build --registry "${REG%%.*}" --image "nexus:$TAG" --image "nexus:latest" \
  --file deploy/Dockerfile .
cd deploy/cloud/azure
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG"
```

> ### ⚠️ Always bump `name_suffix` when rebuilding from scratch
> ACR, Postgres and Valkey take **globally unique** names. `nexusprodacr` was already taken by
> someone else on the first attempt. Worse, Azure **reserves a Redis cache name even after
> deletion** and its own error says releasing it needs a support ticket — so a destroy-and-redeploy
> with the same derived suffix can be permanently blocked by its own predecessor.

## Step 5 — Confirm the infrastructure

Get the **stable** hostname. Not a revision FQDN — a revision hostname stops serving on the next
deploy and returns ACA's own HTML 404, which looks like the application has broken.

```bash
export RG=nexus-prod-rg
export URL="https://$(az containerapp show -n nexus-prod-app -g $RG --query properties.configuration.ingress.fqdn -o tsv)" && echo "$URL"
```

```bash
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/ready")
  echo "[$i/20] /ready -> $code"; [ "$code" = "200" ] && break; sleep 15
done
```

**Do not treat a deploy as finished until `/ready` returns 200.** `/health` only proves the process
started; `/ready` proves migrations ran, `apply_rls.py` created the least-privilege role, and the
app can connect as it. First boot migrates from an empty database — allow 2–3 minutes.

---

# Steps 6–9 — from "infrastructure exists" to "people can use it"

A green `/ready` is not a usable product. There are **no seeded users**, and the four steps below
are what make the site actually live.

## Step 6 — Create the first workspace

The UI cannot do this. `LoginPage.tsx` always calls the OTP flow, so it emails a code — and with no
SMTP yet, that code never arrives and the form waits forever with no explanation.

```bash
curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name":"Your Company","company_slug":"your-company",
  "email":"you@example.com","full_name":"Your Name",
  "password":"REPLACE-WITH-A-STRONG-PASSWORD"}' | python3 -m json.tool
```

A token back = success. **Note the `/api` prefix** — a root-path call returns `405`, not `404`,
because the SPA catch-all answers it.

You can now log in through the browser at `$URL`. Login needs no email; only registration is gated
on OTP.

## Step 7 — Grant platform superadmin

`owner` is the top **workspace** role and grants nothing at platform level. Platform access is
separate and fails closed.

```bash
grep -q '^NEXUS_PLATFORM_ADMIN_EMAILS=' ~/nexus/deploy/.env   && sed -i 's|^NEXUS_PLATFORM_ADMIN_EMAILS=.*|NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com|' ~/nexus/deploy/.env   || echo 'NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com' >> ~/nexus/deploy/.env
grep '^NEXUS_PLATFORM_ADMIN_EMAILS=' ~/nexus/deploy/.env
```

## Step 8 — Configure email (OTP codes and team invites)

```bash
cd ~/nexus/deploy && python3 - <<'PYEOF'
import pathlib
APP_PASSWORD = "PASTE-16-CHAR-APP-PASSWORD"      # Gmail: Account > Security > App passwords
USERNAME     = "you@example.com"
assert "PASTE" not in APP_PASSWORD, "fill APP_PASSWORD first"
values = {
    "NEXUS_SYSTEM_SMTP_PROVIDER":  "gmail",
    "NEXUS_SYSTEM_SMTP_USERNAME":  USERNAME,
    "NEXUS_SYSTEM_SMTP_PASSWORD":  APP_PASSWORD.replace(" ", ""),   # Google displays it spaced
    "NEXUS_SYSTEM_SMTP_FROM":      USERNAME,
    "NEXUS_SYSTEM_SMTP_FROM_NAME": "NEXUS GTM",
}
p = pathlib.Path(".env")
kept = [ln for ln in p.read_text().splitlines()
        if not any(ln.strip().startswith(k + "=") for k in values)]
p.write_text("
".join(kept + [""] + [f"{k}={v}" for k, v in values.items()]) + "
")
print(f"{len(values)} SMTP settings written")
PYEOF
```

Gmail needs an **App Password** with 2FA enabled — a normal account password is rejected. The
`.replace(" ", "")` handles Google's spaced display format; spaces in the value fail auth.

Apply both Step 7 and Step 8 in one deploy:

```bash
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M)
source ~/clouddrive/nexus-env.sh
cd ~/nexus/deploy/cloud/azure && terraform apply -var "name_suffix=$SFX"   -var "alarm_email=ops@example.com" -var "image=$REG/nexus:$TAG"
```

⚠️ `source` is **required** — `TF_VAR_secrets` is a snapshot taken at source time, so an edit
afterwards is invisible without it.

Verify SMTP before trusting it:

```bash
az containerapp exec -n nexus-prod-app -g $RG --command "python -c \"
from nexus.integrations.email_sender import resolve_smtp
from nexus.core.config import get_settings
import smtplib
s = get_settings()
c = resolve_smtp({'provider': s.system_smtp_provider, 'username': s.system_smtp_username, 'password': s.system_smtp_password})
srv = smtplib.SMTP(c['host'], c['port'], timeout=20); srv.starttls()
srv.login(c['username'], c['password']); print('SMTP LOGIN: OK'); srv.quit()\""
```

Only **after** `SMTP LOGIN: OK`, optionally require email verification for new accounts:

```
NEXUS_OTP_REGISTRATION_ENABLED=true
```

> Enabling this closes `/auth/signup` too. With broken SMTP it locks out **both** signup paths.

## Step 9 — Confirm it is genuinely live

```bash
export TOKEN="$(curl -s -X POST "$URL/api/auth/login" -H 'Content-Type: application/json'   -d '{"email":"you@example.com","password":"REPLACE-WITH-A-STRONG-PASSWORD"}'   | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"

for p in /health /ready /metrics; do printf "%-10s %s
" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p")"; done
for p in /api/auth/me /api/accounts /api/inbox /api/settings /api/admin/billing/whoami; do
  printf "%-30s %s
" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p" -H "Authorization: Bearer $TOKEN")"
done
for a in app worker valkey; do
  printf "%-18s %s
" "nexus-prod-$a" "$(az containerapp show -n nexus-prod-$a -g $RG --query properties.runningStatus -o tsv)"
done
```

Everything **200** and all three **Running** = fully live. `403` on `whoami` means Step 7 has not
rolled over yet.

Full detail in [02-SMOKE-TESTS.md](02-SMOKE-TESTS.md) and [04-ENDPOINT-TESTS.md](04-ENDPOINT-TESTS.md).

---

## Expected resource inventory

21 resources. If your count differs, something silently did not create.

```bash
az resource list --resource-group nexus-prod-rg --query "[].{name:name,type:type}" -o table
```

| Resource | Notes |
|---|---|
| Resource group, VNet, 3 subnets | `aca` /23 delegated, `db` /24 delegated, `pe` /24 |
| Private DNS zone + link (postgres) | Redis zone was removed with the retired cache |
| Log Analytics workspace | Set a daily cap — see [07-OPERATIONS.md](07-OPERATIONS.md) |
| Container registry (Basic) | Holds your images; do not delete |
| Container Apps environment | Plus the auto-created `ME_...` managed RG — leave it alone |
| PostgreSQL Flexible Server + database + config | Zone is Azure-assigned, not pinned |
| Container Apps ×3 | app (external), worker (none), valkey (internal TCP) |
| Action group + metric alert | Useless without `alarm_email` |

## Cost

Roughly **$70–105/month** at this shape (estimate, not verified against live pricing). Postgres and
the always-on app replica dominate. Full breakdown and upgrade triggers in
[06-SCALING.md](06-SCALING.md).

Set a budget before you walk away:

**Portal → Cost Management → Budgets** → scope `nexus-prod-rg` → $150/mo → alerts at 50/80/100%.

## Protect it

```bash
az lock create --name protect-prod --lock-type CanNotDelete --resource-group nexus-prod-rg
```

Note this blocks `terraform destroy` too — remove the lock deliberately when you mean to tear down.
