# Azure deployment — Container Apps + managed data (variant b)

Complete Terraform for Infojoy GTM on **Azure Container Apps (ACA)** with **managed data**
(PostgreSQL Flexible Server + Azure Cache for Redis). It mirrors the AWS stack and uses the same
container image and `deploy/.env`.

> **Why managed data on Azure:** stateful Postgres on ACA + Azure Files is fragile (the AWS
> self-hosted-on-Fargate+EFS pattern doesn't translate cleanly). Managed Flexible Server +
> Azure Cache is the ACA-native, production-correct choice — and it matches AWS **variant (a)**.

## Deploy

```bash
ALARM_EMAIL=ops@example.com deploy/cloud/deploy.sh azure app.example.com
```

Prereqs: `terraform` ≥ 1.6, `az` CLI authenticated, and a filled `deploy/.env`. **Docker is not
required** — the image is built server-side with `az acr build`, so this runs unchanged in Azure
Cloud Shell (which has terraform and az preinstalled and no Docker daemon).

`domain` is optional in practice: it provisions nothing, feeding only the
`custom_domain_next_step` output. Until a custom domain is bound the app serves on the
ACA-assigned `*.azurecontainerapps.io` FQDN with a Microsoft-managed certificate.

## First-deploy failures this stack has actually hit

Every one of these was measured on a real subscription, and each is now guarded. Listed because
the guards are easy to remove by accident, and because the error messages mostly do **not** say
what is wrong.

| Symptom | Real cause | Guard |
|---|---|---|
| `400 The value of the 'Version' should be in: []` | `Microsoft.DBforPostgreSQL` unregistered. The **empty list** reads as "PG 16 unsupported here" and sends you to change the version or region — neither is the problem. | `deploy.sh` registers providers first |
| `409 MissingSubscriptionRegistration: Microsoft.App` | Same, for Container Apps | ditto |
| `404 The specified container does not exist` on `terraform init` | The state blob **container** was never created (the storage account exists, so this misleads) | `deploy.sh` preflight creates it |
| `the name "nexusprodacr" ... is already in use` | ACR names are unique across **all of Azure**, and `<project><env>acr` is what every deployment computes | `local.uniq` suffix in `platform.tf` |
| `Name unavailable for reservation` (Redis) | Same, and worse — Azure **reserves a cache name even after deletion**, and releasing it needs a support ticket | `local.redis_name` |
| Alerts never arrive | `alarm_email` empty ⇒ action group with **zero receivers**. The portal still shows an alert rule, so it reads as configured. | `deploy.sh` warns; pass `ALARM_EMAIL` |
| `AuthorizationPermissionMismatch` creating the state container | Subscription **Contributor does not grant blob data access** (needs Storage Blob Data Contributor) | preflight uses the account key, not AAD |

Two more that are configuration, not bugs:

- **`enable_non_ssl_port` vs `non_ssl_port_enabled`** — the argument was renamed in azurerm 4.0.
  `versions.tf` pins `~> 3.110`, so `data.tf` uses the 3.x spelling. **Bumping the provider pin
  means changing that line too.**
- **`CHANGE_ME` is not a value.** `.env.production.example` ships placeholder secrets and claims
  `deploy.sh` replaces them. It now does — previously a plain truthiness test wrote `CHANGE_ME`
  through as the live Postgres admin password and the JWT signing key, neither of which fails
  loudly.

## What it provisions

| Resource | Purpose |
|---|---|
| Resource group + Log Analytics | container + platform logs |
| VNet (aca / db / pe subnets) + private DNS zones | private networking |
| `azurerm_container_app_environment` (VNet-integrated) | the ACA environment |
| ACR (`azurerm_container_registry`) | image registry |
| `azurerm_postgresql_flexible_server` (+ DB, ZoneRedundant HA) | managed Postgres |
| `azurerm_redis_cache` + private endpoint | managed queue/cache |
| `azurerm_container_app` ×2 (app external, worker internal) | API + worker |
| `azurerm_monitor_action_group` + metric alert | alerting |

Connection URLs are composed in `container_apps.tf` from the managed endpoints and injected as ACA
secrets (env var → secret ref). The app runs migrations on boot (`NEXUS_RUN_MIGRATIONS=1` via the
`args`-preserved entrypoint); the worker waits on it.

## Validate first (azurerm isn't installed in the dev box)

```bash
cd deploy/cloud/azure
terraform init && terraform fmt -check && terraform validate
terraform plan -var domain=app.example.com
```

## Follow-ups (documented, not auto-applied)
- **Custom domain + TLS:** CNAME `var.domain` → the app's `latest_revision_fqdn` (output), then add
  `azurerm_container_app_custom_domain` + a managed certificate. ACA outputs the binding hint.
- **Secrets in Key Vault:** swap the inline ACA secrets for Key Vault references via a user-assigned
  managed identity (the hardening path).
- **Worker autoscaling:** add a custom (queue-depth) KEDA scale rule when load warrants.
