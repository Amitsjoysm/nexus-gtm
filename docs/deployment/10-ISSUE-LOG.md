# 10 — Issue log: every failure and its resolution

A complete register of the **25 issues** hit taking this application from an empty Azure
subscription to a live, usable deployment on 2026-08-05/06. Each entry records what was seen, what
was actually wrong, why the two differ, the fix, and how to verify it.

**The single most useful observation:** in 14 of 25 cases the error message named the wrong thing.
`Version should be in: []` meant "this region is capacity-restricted". `405 Method Not Allowed`
meant "wrong URL prefix". `permission denied to alter role` meant "managed Postgres admins are not
superusers". Reading the symptom literally cost more time than any single fix.

Severity key: **S1** silent/dangerous · **S2** blocks deployment · **S3** blocks a feature ·
**S4** cost or hygiene

---

## A. Pre-deployment (found by reading code, before any Azure resource existed)

### A1 — Connection pool hardcoded, budget exceeded by 3.5× · S1

**Symptom:** none. It would have deployed and then failed under load, most likely mid-release.

**Root cause:** pool sizes were literals in [nexus/core/db.py](../../nexus/core/db.py) — `pool_size=10,
max_overflow=20` plus a platform engine at `2/3` = **35 connections per process**. The committed
Terraform ran `--workers 2` at `app_min = 2`: 2 replicas × 2 processes × 35 + worker = **175
connections** against a Burstable B1ms's ~50.

**Why it would have been hard to diagnose:** exceeding `max_connections` does not degrade
gracefully — Postgres refuses new connections and it surfaces as 500s under exactly the load that
caused it. And because Container Apps runs old and new revisions *simultaneously* during a rollout,
app connections **double for the length of a release**. It works in steady state and breaks during
deploys.

**Fix:** made the pools settings-driven (`NEXUS_DB_POOL_SIZE`, `NEXUS_DB_MAX_OVERFLOW`,
`NEXUS_DB_PLATFORM_POOL_SIZE`, `NEXUS_DB_PLATFORM_MAX_OVERFLOW`) with defaults **identical to the
old literals**, so no existing deployment changes behaviour. Set to 5+5 in `var.common_env`.

**Result:** 15/process → 30 steady, 45 during deploy, inside ~50.

**Commands used:**

```bash
# what the app actually resolves, per process
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
from nexus.core.config import get_settings
s = get_settings()
print('per-process:', s.db_pool_size + s.db_max_overflow + s.db_platform_pool_size + s.db_platform_max_overflow)\""

# what Postgres actually allows, and what is in use right now
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def m():
    e = create_async_engine(os.environ['NEXUS_DATABASE_URL'])
    async with e.connect() as c:
        print('max_connections:', (await c.execute(text('show max_connections'))).scalar())
        print('in use         :', (await c.execute(text('select count(*) from pg_stat_activity'))).scalar())
asyncio.run(m())\""

pytest tests/test_db_pool_config.py -q          # 4 passed
```

**Verify:** `tests/test_db_pool_config.py` (4 tests) pins the defaults, that env vars reach
`create_async_engine`, that `pool_pre_ping` stays on, and that SQLite still gets no pool args.

### A2 — `--workers 2` is wrong on Container Apps · S2

**Root cause:** three independent problems, any one sufficient. It doubles DB connections per
replica; it hides load from the `http_scale_rule`, so ACA scales late while the container is
already saturated; and each process keeps its own Prometheus registry, so `/metrics` reports about
half the traffic and flaps between workers unless `PROMETHEUS_MULTIPROC_DIR` is set and swept.

**Fix:** `--workers 1` in `container_apps.tf`. ACA scales by *replica*; in-container process
multiplication fights that. Also removes the need for the multiproc directory entirely.

---

## B. Naming and identity

### B1 — ACR name globally taken · S2

```
the name "nexusprodacr" used for the Container Registry needs to be globally unique
and isn't available: The registry nexusprodacr is already in use.
```

**Root cause:** ACR names are unique across **all of Azure**, and `<project><env>acr` is exactly
what every other nexus/prod deployment computes.

**Fix:** `local.uniq` in `platform.tf`, derived from `sha1(subscription_id)[:8]` — **deterministic,
not `random_string`**. A random suffix would need `keepers` to avoid proposing replacement on every
plan, and getting those wrong destroys the registry your images live in. Deterministic is also
known at plan time, so the name appears in the plan instead of "(known after apply)".

**Commands used:**

```bash
az acr check-name --name nexusprodacr8f13d988 --query "{available:nameAvailable,reason:reason}" -o jsonc
terraform plan -no-color -var "name_suffix=$SFX" | grep -E "name +=" | sort -u
```

### B2 — Redis name permanently reserved · S2 (partially unrecoverable)

```
Name unavailable for reservation!: ... In case you have used same name earlier and deleted
that cache ... please reach out to customer support team for removing the reservation
```

**Root cause:** Azure **reserves a Redis cache name even after the cache is deleted.** A
destroy-and-redeploy with the same derived suffix is blocked by its own predecessor.

**Fix:** `var.name_suffix` — an override that wins over the derived value. **Bump it (`v2`, `v3`, …)
on every rebuild-from-scratch.** Deterministic is right for repeated applies and wrong for a
rebuild.

**Not fully recoverable:** `nexus-prod-redis-8f13d988` and `-v2` are gone. Only new names work.

### B3 — Postgres name would have collided too · S2

Never reached, because the apply died earlier. Fixed pre-emptively with the same `local.uniq`
suffix rather than waiting for it to fail.

---

## C. Subscription and region

### C1 — Resource providers unregistered · S2

```
409 MissingSubscriptionRegistration: The subscription is not registered to use
namespace 'Microsoft.App'
```

**Commands used:**

```bash
for ns in Microsoft.App Microsoft.DBforPostgreSQL Microsoft.Cache \
          Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.Network; do
  az provider register --namespace $ns --wait
done

az provider list \
  --query "[?namespace=='Microsoft.App'||namespace=='Microsoft.DBforPostgreSQL'||namespace=='Microsoft.Cache'].{ns:namespace,state:registrationState}" \
  -o table          # all three must read Registered
```

**Fix:** `deploy.sh` now registers six namespaces with `--wait` before `terraform init`.

### C2 — `Version should be in: []` — three deploys lost to this one · S2

```
400 ParameterOutOfRange: The value of the 'Version' should be in: [].
```

**What it looks like:** PostgreSQL 16 is unsupported in this region.

**What it actually was:** first `Microsoft.DBforPostgreSQL` being unregistered, and then —
after registration — **East US being capacity-restricted for Flexible Server on this
subscription**:

```json
"reason": "Provisioning is restricted in this region. Please choose a different region."
```

**Why this cost so much time:** an *empty* list of valid versions reads as "16 is not offered
here", which sends you to change `version` or the SKU. Neither is the problem. The version is
simply the first parameter Azure validates against a capability set it cannot populate.

**The diagnostic that settles it in ten seconds** — a healthy region returns tens of thousands of
bytes, a restricted one a few thousand:

```bash
for r in eastus eastus2 centralus westus3; do
  echo "$r: $(az postgres flexible-server list-skus -l $r -o json | wc -c) bytes"
done
```

Measured: `eastus` **7,836** bytes (restricted) · `eastus2` **84,390** (healthy).

**Fix:** `location` default → `eastus2`, with the check command written into `variables.tf`. Also
made `pg_version` a variable so a genuine version problem is a one-line change.

**Commands used:**

```bash
# 1. is the provider even registered?
az provider show --namespace Microsoft.DBforPostgreSQL --query registrationState -o tsv

# 2. THE decisive check — byte count per region
for r in eastus eastus2 centralus westus2 westus3; do
  echo "$r: $(az postgres flexible-server list-skus -l $r -o json 2>/dev/null | wc -c) bytes"
done

# 3. read the reason a small response gives
az postgres flexible-server list-skus -l eastus -o json | head -c 400

# 4. which regions this subscription may use at all
az provider show --namespace Microsoft.DBforPostgreSQL \
  --query "resourceTypes[?resourceType=='flexibleServers'].locations | [0]" -o tsv
```

**Cost of the change:** everything is regional and Postgres must share a region with its delegated
subnet — so this was a full rebuild, not a move.

---

## D. Terraform state and tooling

### D1 — State container missing · S2

```
Failed to get existing workspaces: ... 404 The specified container does not exist.
```

The storage **account** existed; only the blob container did not. The error names neither the
account nor the fix. **Fix:** preflight in `deploy.sh`.

### D2 — `AuthorizationPermissionMismatch` creating the container · S2

**Root cause:** subscription **Contributor does not grant blob data access** — that is a separate
role (`Storage Blob Data Contributor`). So `--auth-mode login` fails for operators who can
otherwise deploy the entire stack.

**Fix:** the preflight uses the account key.

**Commands used:**

```bash
SA=nexustfstate05082026
KEY="$(az storage account keys list --account-name "$SA" \
        --resource-group nexus-tfstate-rg --query '[0].value' -o tsv)"

az storage container create --name tfstate --account-name "$SA" --account-key "$KEY"
az storage container list --account-name "$SA" --account-key "$KEY" --query "[].name" -o tsv
terraform init                    # now gets past the backend
```

### D3 — `TF_VAR_secrets` lost on every Cloud Shell reconnect · S2

Terraform's validator refuses to run without it. **That is correct behaviour, not friction** —
without it Terraform would point the app at a role `apply_rls.py` never creates, and the obvious
"fix" (owner URL) silently removes RLS for every tenant.

**Fix:** `~/clouddrive/nexus-env.sh`, sourced at session start. See
[07-OPERATIONS.md](07-OPERATIONS.md).

**Commands used:**

```bash
source ~/clouddrive/nexus-env.sh
echo "$TF_VAR_secrets" | python3 -c "import json,sys; d=json.load(sys.stdin); print('keys:', len(d)); print('app pw len:', len(d.get('NEXUS_APP_DB_PASSWORD','')))"
# app pw len must be 48 — 0 means .env did not parse
```

### D4 — Cloud Shell has no Docker daemon · S2

**Fix:** `az acr build` on the azure path — builds server-side, and is the same mechanism
`azure-pipelines-ci.yml` uses so operator and CI deploys produce images identically.

---

## E. Provider and API drift

### E1 — Azure Cache for Redis is retired · S2, architectural

```
400 BadRequest: Azure Cache for Redis is retiring, create Azure Managed Redis instance instead.
```

**Root cause:** the resource **type** is refused, not an argument. Renaming nothing helps.

**Options weighed:** Azure Managed Redis ($40–90/mo, or ~$300 if the azurerm 3.x pin exposes only
`Enterprise_E*` SKUs) against a ~$90/mo total budget — for a component used in exactly two places,
the job queue and the idempotency store.

**Fix:** Valkey as an internal-only Container App (~$10/mo), same `valkey:8-alpine` image
`docker-compose.prod.yml` already runs. `external_enabled = false`, TCP ingress on 6379,
`min == max == 1`.

**Commands used:**

```bash
# what the Microsoft.Cache namespace still offers
az provider show --namespace Microsoft.Cache --query "resourceTypes[].resourceType" -o tsv

# after switching to Valkey — prove the app can reach it
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
import os, redis
print('url :', os.environ['NEXUS_REDIS_URL'])
print('ping:', redis.from_url(os.environ['NEXUS_REDIS_URL']).ping())\""
```

**Trade accepted knowingly:** no persistence (`--save ''`, ephemeral storage). A restart loses
in-flight one-shot jobs. Sweeps re-enqueue idempotently and failures dead-letter to Postgres, so
the exposure is bounded — but container restarts are more frequent than managed-node failures.

### E2 — `non_ssl_port_enabled` vs `enable_non_ssl_port` · S2

The argument was renamed in azurerm **4.0**; `versions.tf` pins `~> 3.110`. **The two are coupled** —
bumping the provider pin means changing that line too, and `data.tf` says so.

### E3 — azurerm crashes on an empty string in `args` · S2

```
interface conversion: interface {} is nil, not string
```

Visible in the error as `--save <nil>`. Valkey disables snapshotting via an **empty-string
argument**, and an empty list element crashes the provider before the request reaches Azure.

**Fix:** `command = ["sh", "-c", "exec valkey-server --save '' ..."]` — the empty argument lives
inside a shell string the provider can serialise. `exec` matters: without it `sh` stays PID 1 and
swallows SIGTERM, so ACA waits out the full grace period on every restart.

---

## F. Terraform fighting Azure's server-side defaults

Three instances of one pattern: **Azure populates a field, config leaves it null, Terraform
proposes a change forever.**

### F1 — ACA environment replaced on EVERY apply · S1, the most dangerous of all

```
- infrastructure_resource_group_name = "ME_nexus-prod-aca_nexus-prod-rg_eastus2" -> null
  # forces replacement
```

**Why this is the worst one in this document:** it **succeeds**. The deploy works. But replacing
the environment takes ~10 minutes to delete plus ~3 to recreate, and **cascades** — every
`azurerm_container_app` in it is replaced too, because `container_app_environment_id` becomes
"known after apply". Once the app is serving traffic, that is a full outage on every routine
deploy, triggered by an innocuous change.

**It cannot be set in config either** — the provider only accepts it alongside a `workload_profile`
block, which a Consumption-only environment does not have.

**Fix:** `lifecycle { ignore_changes = [infrastructure_resource_group_name, tags] }`.

**Commands used:**

```bash
# THE diagnostic — name the attribute instead of guessing
terraform plan -no-color -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG" 2>&1 \
  | grep -B5 -A45 "container_app_environment.main must be replaced" \
  | grep "forces replacement"

# confirm nothing is live in the environment before allowing a replacement
terraform state list | grep container_app

# after the fix — must show 0 to destroy and no replacement lines
terraform plan -no-color -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG" 2>&1 | grep -E "^Plan:|must be replaced"
```

**How it was found:** the `# forces replacement` line in `terraform plan`. It was requested twice
before being read; two unnecessary environment rebuilds were the cost of working around the symptom
instead of diagnosing it.

### F2 — Postgres `zone` · S2

```
`zone` can only be changed when exchanged with the zone specified in
`high_availability.0.standby_availability_zone`
```

Introduced by fixing something else: `zone = "1"` was commented out (pinning a zone can make an
otherwise-valid SKU unprovisionable), but Azure had already assigned zone 1. Config null vs actual
`1` = a change Azure refuses.

**Fix:** `lifecycle { ignore_changes = [zone] }`. Hardcoding just moves the problem — the correct
value is whatever Azure picked, which is not knowable before the create.

### F3 — `workload_profile_name` on container apps · S4

Azure defaults it to `"Consumption"`; config does not set it, so every plan shows
`"Consumption" -> null`. Same `ignore_changes` treatment.

---

## G. Application-level

### G1 — `apply_rls.py` fails: managed Postgres admins are not superusers · S2

```
InsufficientPrivilegeError: permission denied to alter role
DETAIL: Only roles with the SUPERUSER attribute may change the SUPERUSER attribute.
[SQL: ALTER ROLE nexus_app WITH LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS ...]
```

**Root cause:** Azure's admin is a member of `azure_pg_admin`, not a superuser. Postgres refuses
`NOSUPERUSER`/`NOBYPASSRLS` from a non-superuser **even when setting them to the value the role
already has**.

**The lazy fix is wrong.** Both are `CREATE ROLE` defaults, so deleting them would work — but they
are precisely the attributes that decide whether RLS is a real tenant boundary or decoration. A
role with `BYPASSRLS` ignores every policy the script creates and **nothing downstream reports it**;
cross-tenant reads simply succeed.

**Fix:** try the explicit form (still correct on self-hosted Postgres), fall back to what a managed
admin can set, then **query `pg_roles` and fail the deploy** if `rolsuper` or `rolbypassrls` is
true. Verified, not assumed.

**Commands used:**

```bash
# see exactly where the entrypoint stopped
az containerapp logs show --name nexus-prod-app --resource-group nexus-prod-rg --tail 100 \
  | grep -A5 "apply_rls"

# after the fix + image rebuild — verify the role really is constrained
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def m():
    e = create_async_engine(os.environ['NEXUS_DATABASE_URL'])
    async with e.connect() as c:
        print('current_user:', (await c.execute(text('select current_user'))).scalar())
        r = (await c.execute(text('select rolsuper, rolbypassrls from pg_roles where rolname=current_user'))).first()
        print('rolsuper:', r[0], 'rolbypassrls:', r[1])     # both must be False
asyncio.run(m())\""
```

### G2 — `CHANGE_ME` written through as live credentials · S1

`.env.production.example` ships placeholder secrets and says `deploy.sh` replaces them. It did not
— a plain truthiness test treats `"CHANGE_ME"` as a real value, so it became the **Postgres admin
password** and the **JWT signing key**. Neither fails loudly: Postgres accepts any password, and
the config validator rejects only the one exact dev-default string. The value is in the public
repository.

**Fix:** a `PLACEHOLDERS` set in `build_secrets`, plus generation of `NEXUS_SECRET_KEY` (previously
only the two DB passwords were generated).

### G3 — `405 Method Not Allowed` on every API call · S3

**Root cause:** every router is mounted under **`/api`** ([main.py:204](../../nexus/main.py:204)).
Only `/health`, `/ready`, `/metrics`, `/docs` and `/openapi.json` are at the root. A request to
`/auth/signup` falls through to the SPA static mount, registered last as a catch-all, which serves
GET only — hence 405 rather than 404. That reads as "endpoint exists, wrong method", sending you to
inspect the route instead of the path.

**Commands used:**

```bash
# ask the running app what its paths actually are
curl -s "$URL/openapi.json" | python3 -c "
import json,sys
d = json.load(sys.stdin)
for p, o in sorted(d['paths'].items()):
    print(' '.join(m.upper() for m in o), p)" | head -40

curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{...}'
```

### G4 — `Azure Container App - Unavailable` HTML 404 · S3

**Root cause:** `outputs.tf` emitted `latest_revision_fqdn`, which embeds the revision name. The
moment a new revision replaced it, that hostname stopped serving and ACA answered with its own
error page — which looks like the application is broken.

**Fix:** `ingress[0].fqdn`, the stable per-app hostname. Also the correct CNAME target: a DNS record
pointing at a revision would break on the next release.

**Commands used:**

```bash
# the stable hostname — always routes to whatever revision is live
export URL="https://$(az containerapp show -n nexus-prod-app -g nexus-prod-rg \
  --query properties.configuration.ingress.fqdn -o tsv)" && echo "$URL"

curl -s -o /dev/null -w "ready: %{http_code}\n" "$URL/ready"
```

### G5 — No seeded users, and the UI cannot create the first one · S3

The `users` and `tenants` tables are empty on a fresh deployment — deliberately, since a default
admin with credentials in a public repo is how self-hosted products get compromised.

But `LoginPage.tsx` calls **`registerStart`** (the OTP flow) and never `/auth/signup`, so the
browser always requests an emailed code regardless of `NEXUS_OTP_REGISTRATION_ENABLED`. With no
SMTP configured, **no code is ever sent and there is no on-screen explanation** — the form waits
forever.

**Fix:** create the first workspace via `POST /api/auth/signup`, then configure SMTP. See
[03-SEED-USERS.md](03-SEED-USERS.md).

**Commands used:**

```bash
# create the first workspace — the UI cannot
curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name":"MarketJoy","company_slug":"marketjoy",
  "email":"you@example.com","full_name":"Your Name","password":"<strong-password>"}' \
  | python3 -m json.tool

# SMTP — verify BEFORE trusting it, and before enabling OTP registration
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
from nexus.integrations.email_sender import resolve_smtp
from nexus.core.config import get_settings
import smtplib
s = get_settings()
c = resolve_smtp({'provider': s.system_smtp_provider, 'username': s.system_smtp_username,
                  'password': s.system_smtp_password})
srv = smtplib.SMTP(c['host'], c['port'], timeout=20); srv.starttls()
srv.login(c['username'], c['password']); print('SMTP LOGIN: OK'); srv.quit()\""
```

### G6 — Platform superadmin is not a tenant role · S3 (working as designed)

`owner` is the top **workspace** role and grants nothing at platform level. Platform membership
comes from `NEXUS_PLATFORM_ADMIN_EMAILS` or the `platform_admins` table, and fails closed —
otherwise any customer who signs up would gain platform-wide power.

**Commands used:**

```bash
grep -q '^NEXUS_PLATFORM_ADMIN_EMAILS=' ~/nexus/deploy/.env \
  && sed -i 's|^NEXUS_PLATFORM_ADMIN_EMAILS=.*|NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com|' ~/nexus/deploy/.env \
  || echo 'NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com' >> ~/nexus/deploy/.env

source ~/clouddrive/nexus-env.sh                     # REQUIRED — re-reads .env
cd ~/nexus/deploy/cloud/azure && terraform apply -var "name_suffix=$SFX" \
  -var "alarm_email=ops@example.com" \
  -var "image=$(az containerapp show -n nexus-prod-app -g nexus-prod-rg \
                 --query 'properties.template.containers[0].image' -o tsv)"

curl -s "$URL/api/admin/billing/whoami" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### G7 — Alerts configured but notifying nobody · S1

`alarm_email` empty ⇒ `azurerm_monitor_action_group` created with **zero receivers**. The portal
still shows an alert rule, so it reads as configured. Worse than no alert.

**Fix:** `deploy.sh` warns loudly with the exact re-run command.

**Commands used:**

```bash
# empty output = the alert notifies nobody
az monitor action-group show -n nexus-prod-alerts -g nexus-prod-rg \
  --query "emailReceivers[].emailAddress" -o tsv

terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" \
  -var "image=$REG/nexus:$TAG"
```

### G8 — ICP website analysis silently returns nothing · S3

**Symptom:** *"Couldn't analyze that site. Fill the fields manually, or check that search/LLM keys
are configured."* — with every key correctly configured.

**Diagnosis:** Exa returned 200 with 10 hits, blob 9,870 chars, Anthropic returned 200 — and the
draft was still empty. The response measured **2,996 characters against `max_tokens=800`**, i.e.
truncated at the cap, mid-string. A truncated JSON object has no closing brace, so the extraction
regex matched nothing. The model also wrapped its output in ```` ```json ```` fences despite the
prompt forbidding them.

**Why it was invisible:** `analyze_website_to_icp` has four `_empty_draft()` exits and **none of
them logged**. Truncation, a fenced response, and a genuinely uninformative website all produced
the same blank form and the same toast.

**Fix:** `max_tokens` 800 → 2000; strip code fences before parsing; log every failure path with the
response length — which is what identifies truncation. Plus a warning when JSON parses cleanly but
coerces to empty, which catches the model answering with its own key names
(`geographies`/`roles` instead of `countries`/`buyer_titles`).

---

**Commands used:**

```bash
# 1. are the providers real, or has the chain fallen through to the stub?
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
from nexus.core.config import get_settings
s = get_settings()
print('llm_provider   :', s.llm_provider)
print('  anthropic key:', bool(s.anthropic_api_key))
print('  groq keys    :', len(s.groq_api_key_list))
print('search_provider:', s.search_provider)
print('  exa keys     :', len(s.exa_api_key_list))
print('  apify keys   :', len(s.apify_api_key_list))\""

# 2. run the real path with DEBUG — prints provider TYPES and the response length
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
import asyncio, logging
logging.basicConfig(level=logging.DEBUG)
from nexus.agents.llm import get_llm_provider
from nexus.integrations.registry import get_registry
from nexus.relevance.website_icp import analyze_website_to_icp
async def m():
    s = get_registry().search; l = get_llm_provider()
    print('SEARCH PROVIDER:', type(s).__name__)
    print('LLM PROVIDER   :', type(l).__name__)
    print('DRAFT:', await analyze_website_to_icp('example.com', search=s, llm=l))
asyncio.run(m())\""

# 3. test a key directly — present is not the same as accepted
az containerapp exec -n nexus-prod-app -g nexus-prod-rg --command "python -c \"
import os, httpx
r = httpx.post('https://api.exa.ai/search',
    headers={'x-api-key': os.environ.get('NEXUS_EXA_API_KEY','')},
    json={'query':'test','numResults':1}, timeout=20)
print('exa status:', r.status_code)\""

# 4. after the fix — every failure path now names itself
az containerapp logs show -n nexus-prod-app -g nexus-prod-rg --tail 100 | grep website_icp
```

## Patterns worth carrying forward

1. **Read the plan, not the symptom.** F1 was diagnosable from one `# forces replacement` line and
   was worked around twice before being read.
2. **An empty result is not an empty answer.** C2's empty version list, G8's empty draft, and RLS's
   zero-row cross-tenant reads all mean "we could not look", not "there is nothing there".
3. **Silence is the expensive failure mode.** A1, G2, G7 and G8 all *worked* while doing the wrong
   thing. Every fix in this document that adds a log line or a verification query is there because
   the absence of one cost hours.
4. **Verify, do not assume defaults.** G1's `pg_roles` check exists because "it's the default" is
   not good enough for the two attributes that make RLS real.
5. **Deploy-time behaviour differs from steady state.** A1 and F1 are both only dangerous during a
   rollout, when old and new run together.
