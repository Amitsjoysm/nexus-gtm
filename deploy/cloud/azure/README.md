# Azure deployment — Container Apps + managed data (variant b)

Complete Terraform for Infojoy GTM on **Azure Container Apps (ACA)** with **managed data**
(PostgreSQL Flexible Server + Azure Cache for Redis). It mirrors the AWS stack and uses the same
container image and `deploy/.env`.

> **Why managed data on Azure:** stateful Postgres on ACA + Azure Files is fragile (the AWS
> self-hosted-on-Fargate+EFS pattern doesn't translate cleanly). Managed Flexible Server +
> Azure Cache is the ACA-native, production-correct choice — and it matches AWS **variant (a)**.

## Deploy

```bash
deploy/cloud/deploy.sh azure app.example.com
```

Prereqs: `terraform` ≥ 1.6, `docker`, `az` CLI authenticated (`az login`), and a filled `deploy/.env`.
The script provisions the registry, builds + pushes the image to ACR, then applies the full stack.

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
