# 11 — Destroy and rebuild: exact commands

Copy-paste runbook for tearing the stack down and bringing it back **fully live** — infrastructure,
a working login, superadmin, and email. Roughly 60 minutes end to end, most of it waiting.

Everything runs in **Azure Cloud Shell** (`>_` in the portal). Nothing to install.

---

## Before you destroy — three things that do not come back

| | |
|---|---|
| **`deploy/.env`** | Terraform does not manage it. Losing it loses your DB credentials and every API key. **Download it out of Cloud Shell first.** |
| **Redis/ACR/Postgres names** | Azure **permanently reserves a Redis cache name after deletion**. You MUST use a new `name_suffix` on rebuild. |
| **Postgres data** | `terraform destroy` deletes the server and its automatic backups. Take a dump if the data matters. |

```bash
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M) && ls -la ~/clouddrive/nexus-env-backup-*
```

Then use Cloud Shell **⇅ → Download** on `~/nexus/deploy/.env` and store it in a password manager.

If the data matters:

```bash
az containerapp exec -n nexus-prod-app -g nexus-prod-rg \
  --command "sh -c 'pg_dump \"\$NEXUS_DB_OWNER_URL\" --no-owner --format=custom'" \
  > ~/clouddrive/nexus-$(date +%F).dump && ls -lh ~/clouddrive/nexus-*.dump
```

---

# PART 1 — DESTROY

### 1.1 Restore session variables

```bash
source ~/clouddrive/nexus-env.sh
```

Expect `restored: 3x secrets`. **Destroy fails the validator without this.**

### 1.2 Remove the delete lock, if you set one

```bash
az lock delete --name protect-prod --resource-group nexus-prod-rg 2>/dev/null; echo "lock cleared"
```

### 1.3 Destroy

```bash
cd ~/nexus/deploy/cloud/azure && terraform destroy -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com"
```

Type `yes`. **10–20 minutes** — the ACA environment alone takes ~10 to delete. **Do not interrupt.**

### 1.4 Confirm

```bash
az resource list --resource-group nexus-prod-rg -o table 2>/dev/null || echo "resource group gone — clean"
az group list --query "[?starts_with(name,'nexus')].name" -o tsv
```

`nexus-tfstate-rg` **must survive** — Terraform does not manage it, and it holds your state. If
`ME_nexus-prod-aca_...` lingers, delete it manually; it is orphaned once the environment is gone.

---

# PART 2 — REBUILD TO FULLY LIVE

### 2.1 Pick a region that actually offers Postgres

**Do not skip this.** Three deploys were lost to a capacity-restricted region reporting itself as
an empty version list.

```bash
for r in eastus2 centralus westus3 eastus; do
  echo "$r: $(az postgres flexible-server list-skus -l $r -o json 2>/dev/null | wc -c) bytes"
done
```

Healthy ≈ **80,000+ bytes**. Restricted ≈ **8,000**. Use a healthy one; `eastus2` is the default.

### 2.2 Upload and unpack the code

Cloud Shell toolbar → **⇅ Upload** → `nexus-deploy.zip`, then:

```bash
mkdir -p ~/nexus && unzip -q -o ~/nexus-deploy.zip -d ~/nexus && cd ~/nexus && \
grep -c "ignore_changes" deploy/cloud/azure/platform.tf deploy/cloud/azure/data.tf && \
grep -c "max_tokens=2000" nexus/relevance/website_icp.py
```

All three must be ≥ 1. If any is `0`, an older zip is still in place.

### 2.3 Restore `.env`

```bash
LATEST="$(ls -t ~/clouddrive/nexus-env-backup-* | head -1)" && echo "restoring: $LATEST" && \
mkdir -p ~/nexus/deploy && cp "$LATEST" ~/nexus/deploy/.env && \
grep -E "^(POSTGRES_PASSWORD|NEXUS_APP_DB_PASSWORD|NEXUS_SECRET_KEY)=" ~/nexus/deploy/.env | awk -F= '{print $1, length($2)" chars"}'
```

All three must be **48 chars**. If any is shorter, that backup predates the secret bake — pick an
older one, or regenerate (see [01-LAUNCH.md](01-LAUNCH.md#step-3--secrets)).

Confirm the settings that make the site *usable*, not just running:

```bash
grep -E "^(NEXUS_PLATFORM_ADMIN_EMAILS|NEXUS_OTP_REGISTRATION_ENABLED|NEXUS_SYSTEM_SMTP_USERNAME)=" ~/nexus/deploy/.env
```

Missing `NEXUS_PLATFORM_ADMIN_EMAILS` ⇒ nobody can reach `/api/admin`. Missing SMTP ⇒ no OTP, no
team invites.

### 2.4 New name suffix — mandatory

```bash
export SFX=v4      # NEVER reuse a previous suffix: Azure reserves deleted Redis names
export RG=nexus-prod-rg
echo "suffix=$SFX"
```

### 2.5 Load secrets and initialise

```bash
source ~/clouddrive/nexus-env.sh && cd ~/nexus/deploy/cloud/azure && terraform init -reconfigure && terraform validate
```

### 2.6 Plan — read it before approving

```bash
terraform plan -no-color -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com" -out=tfplan 2>&1 | tail -3
terraform show -no-color tfplan | grep -E "^Plan:|location +=|sku_name|min_replicas|max_replicas" | sort -u
```

Expect **~20 to add, 0 to change, 0 to destroy**, `location = "eastus2"`,
`B_Standard_B1ms`, app `min 1 / max 3`.

### 2.7 Registry first, then build the image on ACR

```bash
terraform apply -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com" -target=azurerm_container_registry.main
```

```bash
export REG="$(terraform output -raw acr_login_server)" && \
sed -i "s|^export REG=.*|export REG=\"$REG\"|" ~/clouddrive/nexus-env.sh && \
sed -i "s|^export SFX=.*|export SFX=\"$SFX\"|" ~/clouddrive/nexus-env.sh && echo "$REG"
```

(That also updates the helper so a reconnect restores the right values.)

```bash
cd ~/nexus && export TAG="$(date +%Y%m%d-%H%M)" && \
az acr build --registry "${REG%%.*}" --image "nexus:$TAG" --image "nexus:latest" --file deploy/Dockerfile . && \
echo "IMAGE TAG: $TAG   <-- record this, it is your rollback handle"
```

**6–12 minutes.** Builds server-side; Cloud Shell has no Docker.

### 2.8 Full apply

```bash
cd ~/nexus/deploy/cloud/azure && terraform apply -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com" -var "image=$REG/nexus:$TAG"
```

**15–25 minutes.** Postgres 5–10, ACA environment 3–5. **Do not interrupt** — a killed apply leaves
resources Azure knows about that state does not.

### 2.9 Get the STABLE url

```bash
export URL="https://$(az containerapp show -n nexus-prod-app -g $RG --query properties.configuration.ingress.fqdn -o tsv)" && echo "$URL"
```

⚠️ Use this, not a revision FQDN. A revision hostname stops serving on the next deploy and returns
ACA's own HTML 404, which looks like the app is broken.

### 2.10 Wait for readiness

```bash
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/ready")
  echo "[$i/20] /ready -> $code"; [ "$code" = "200" ] && break; sleep 15
done
```

**200 proves** migrations ran, `apply_rls.py` created the least-privilege role, and the app
connects as it. First boot migrates from empty, so allow 2–3 minutes.

If it never reaches 200:

```bash
az containerapp logs show --name nexus-prod-app --resource-group $RG --tail 100
```

### 2.11 Restore data (only if you took a dump)

```bash
az containerapp exec -n nexus-prod-app -g $RG --command "sh -c 'pg_restore --clean --if-exists --no-owner -d \"\$NEXUS_DB_OWNER_URL\"'" < ~/clouddrive/nexus-<date>.dump
```

**Skip to 2.12 if you restored data** — your users already exist.

### 2.12 Create the first workspace

```bash
curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name":"MarketJoy","company_slug":"marketjoy",
  "email":"amit@marketjoy.com","full_name":"Amit Singh",
  "password":"REPLACE-WITH-A-STRONG-PASSWORD"}' | python3 -m json.tool
```

A token back = success. Note the `/api` prefix — a root-path call returns `405`, not `404`.

### 2.13 Verify it is genuinely live

```bash
export TOKEN="$(curl -s -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"amit@marketjoy.com","password":"REPLACE-WITH-A-STRONG-PASSWORD"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"

for p in /health /ready /metrics; do printf "%-10s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p")"; done
for p in /api/auth/me /api/accounts /api/inbox /api/settings /api/admin/billing/whoami; do
  printf "%-30s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p" -H "Authorization: Bearer $TOKEN")"
done
for a in app worker valkey; do
  printf "%-18s %s\n" "nexus-prod-$a" "$(az containerapp show -n nexus-prod-$a -g $RG --query properties.runningStatus -o tsv)"
done
```

**Fully live looks like this:**

```
/health    200
/ready     200
/metrics   200
/api/auth/me                   200
/api/accounts                  200
/api/inbox                     200
/api/settings                  200
/api/admin/billing/whoami      200      <- platform superadmin works
nexus-prod-app     Running
nexus-prod-worker  Running
nexus-prod-valkey  Running
```

`403` on `whoami` ⇒ your email is not in `NEXUS_PLATFORM_ADMIN_EMAILS`, or the revision has not
rolled over yet.

### 2.14 Confirm the queue

```bash
az containerapp exec -n nexus-prod-app -g $RG --command "python -c \"
import os, redis; print('valkey ping:', redis.from_url(os.environ['NEXUS_REDIS_URL']).ping())\""
```

### 2.15 Confirm email

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

### 2.16 Protect it

```bash
az lock create --name protect-prod --lock-type CanNotDelete --resource-group $RG
cp ~/nexus/deploy/.env ~/clouddrive/nexus-env-backup-$(date +%Y%m%d-%H%M)
```

Then **Portal → Cost Management → Budgets** → scope `nexus-prod-rg` → $150/mo → 50/80/100%.

---

## The whole thing, condensed

Once `.env` is in place and `$SFX` is new:

```bash
source ~/clouddrive/nexus-env.sh
cd ~/nexus/deploy/cloud/azure && terraform init -reconfigure
terraform apply -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com" -target=azurerm_container_registry.main
export REG="$(terraform output -raw acr_login_server)"
cd ~/nexus && export TAG="$(date +%Y%m%d-%H%M)"
az acr build --registry "${REG%%.*}" --image "nexus:$TAG" --image "nexus:latest" --file deploy/Dockerfile .
cd deploy/cloud/azure && terraform apply -var "name_suffix=$SFX" -var "alarm_email=amit@marketjoy.com" -var "image=$REG/nexus:$TAG"
export URL="https://$(az containerapp show -n nexus-prod-app -g nexus-prod-rg --query properties.configuration.ingress.fqdn -o tsv)"
echo "$URL"
```

Or, with all preflights automated:

```bash
ALARM_EMAIL=amit@marketjoy.com AZURE_LOCATION=eastus2 TF_VAR_name_suffix=$SFX ~/nexus/deploy/cloud/deploy.sh azure app.example.com
```

---

## Checklist

- [ ] `.env` downloaded **off** Cloud Shell
- [ ] `pg_dump` taken if data matters
- [ ] Delete lock removed
- [ ] `nexus-tfstate-rg` survived the destroy
- [ ] Region verified with `list-skus`
- [ ] **New `name_suffix`**
- [ ] `terraform plan` shows `0 to destroy`
- [ ] Image tag recorded
- [ ] `/ready` = 200
- [ ] Workspace created, login works in a browser
- [ ] `/api/admin/billing/whoami` = 200
- [ ] Valkey ping True · SMTP login OK
- [ ] Delete lock re-applied · budget set · `.env` backed up
