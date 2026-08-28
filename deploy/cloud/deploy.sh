#!/usr/bin/env bash
# Single-script cloud deploy for Infojoy GTM.
#
#   deploy/cloud/deploy.sh aws   app.example.com            # AWS ECS Fargate (self-hosted data)
#   USE_MANAGED_DATA=true \
#   deploy/cloud/deploy.sh aws   app.example.com            # AWS + RDS Multi-AZ + ElastiCache  (variant a)
#   deploy/cloud/deploy.sh azure app.example.com            # Azure Container Apps + managed data (variant b)
#   deploy/cloud/deploy.sh <cloud> <domain> destroy         # tear down
#
# Prereqs: terraform >= 1.6 and the cloud CLI (aws / az) authenticated. Secrets come from
# deploy/.env (gitignored); Terraform composes the DB/Redis URLs from the real endpoints.
#
# DOCKER IS REQUIRED FOR THE AWS PATH ONLY. The azure path builds server-side with `az acr build`,
# so it runs unchanged in Azure Cloud Shell — which has terraform and az preinstalled and no
# Docker daemon. That is the zero-install way to deploy this.
set -euo pipefail

CLOUD="${1:-}"
DOMAIN="${2:-}"
ACTION="${3:-apply}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$REPO_ROOT/deploy/.env"
PROJECT="${PROJECT:-nexus}"
REGION="${AWS_REGION:-us-east-1}"
LOCATION="${AZURE_LOCATION:-eastus2}"

{ [ -z "$CLOUD" ] || [ -z "$DOMAIN" ]; } && { echo "usage: deploy.sh <aws|azure> <domain> [destroy]"; exit 1; }
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (fill secrets; gitignored)"; exit 1; }
[ "$CLOUD" = "aws" ] || [ "$CLOUD" = "azure" ] || { echo "unknown cloud '$CLOUD' (aws|azure)"; exit 1; }

TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || date +%s)"

# ---- RAW secrets from deploy/.env (cloud-agnostic). Terraform composes connection URLs. ----
build_secrets() {
  python - "$ENV_FILE" <<'PY'
import json, sys, secrets as pysecrets
env = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()
skip = {"NEXUS_ENV", "NEXUS_QUEUE_BACKEND", "NEXUS_RUN_MIGRATIONS", "DOMAIN", "ACME_EMAIL",
        "NEXUS_DATABASE_URL", "NEXUS_DB_OWNER_URL", "NEXUS_WORKER_DATABASE_URL", "NEXUS_REDIS_URL"}

# A PLACEHOLDER IS NOT A VALUE. .env.production.example ships these keys set to CHANGE_ME and
# tells the operator that deploy.sh will generate them — but a plain truthiness test treats
# "CHANGE_ME" as a real secret, so it was written straight through as the Postgres admin
# password and the JWT signing key. Both are catastrophic and NEITHER FAILS LOUDLY: Postgres
# accepts whatever password you hand it, and the config validator rejects only the one exact
# dev-default string, not this one. The value is in the repo, so anyone who can read the
# codebase can forge a token for any tenant.
PLACEHOLDERS = {"change_me", "changeme", "todo", "tbd", "xxx", "replace_me", "<generated>"}


def is_real(v):
    return bool(v) and v.strip().lower() not in PLACEHOLDERS


out = {k: v for k, v in env.items() if k not in skip and is_real(v)}

# Generated, never prompted. None of these means anything to a human, and every one is a
# credential that must be long, random and unique. 24 bytes hex = 48 chars, which also clears
# the >=16-char guard on NEXUS_APP_DB_PASSWORD in deploy/cloud/azure/variables.tf.
#
# NEXUS_SECRET_KEY is included deliberately: without it the app falls back to the insecure
# default and Settings._reject_insecure_prod refuses to start. That is a SAFE failure, but it
# is a crash-loop an operator has to diagnose — generating it is strictly better.
for _key in ("POSTGRES_PASSWORD", "NEXUS_APP_DB_PASSWORD", "NEXUS_SECRET_KEY"):
    out.setdefault(_key, pysecrets.token_hex(24))
print(json.dumps(out))
PY
}
export TF_VAR_secrets="$(build_secrets)"

build_and_push() { # <image-uri>
  docker build -f "$REPO_ROOT/deploy/Dockerfile" -t "$1" -t "${1%:*}:latest" "$REPO_ROOT"
  docker push "$1"
  docker push "${1%:*}:latest"
}

# ============================ AWS (ECS Fargate) ============================
if [ "$CLOUD" = "aws" ]; then
  ZONE="${DOMAIN#*.}"
  cd "$HERE/aws"
  TF=(-var "project=$PROJECT" -var "region=$REGION" -var "domain=$DOMAIN" -var "route53_zone_name=$ZONE")
  [ "${USE_MANAGED_DATA:-false}" = "true" ] && TF+=(-var "use_managed_data=true")  # variant (a)
  terraform init -input=false
  if [ "$ACTION" = "destroy" ]; then terraform destroy "${TF[@]}" -auto-approve; exit 0; fi

  echo ">> creating ECR..."
  terraform apply "${TF[@]}" -target=aws_ecr_repository.app -auto-approve -input=false
  REG="$(terraform output -raw ecr_repository_url)"
  echo ">> build + push $REG:$TAG"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REG%%/*}"
  build_and_push "$REG:$TAG"
  echo ">> full apply..."
  terraform apply "${TF[@]}" -var "image=$REG:$TAG" -auto-approve -input=false
  echo; echo "Done. App: $(terraform output -raw app_url)"
  exit 0
fi

# ============================ Azure (Container Apps) ============================
cd "$HERE/azure"
TF=(-var "project=$PROJECT" -var "location=$LOCATION" -var "domain=$DOMAIN")

# Alert routing. azurerm_monitor_action_group creates its email receiver only when this is
# non-empty, so an unset value produces an action group with ZERO RECEIVERS and a metric alert
# that fires into the void. That is worse than having no alert at all: the portal shows an alert
# rule, so it reads as configured. Warn loudly rather than failing — a first deploy should not be
# blocked on an ops address, but nobody should discover this during an incident.
if [ -n "${ALARM_EMAIL:-}" ]; then
  TF+=(-var "alarm_email=$ALARM_EMAIL")
else
  echo ">> WARNING: ALARM_EMAIL is not set. The action group will have no recipients and the"
  echo ">>          container-restart alert will notify NOBODY. Re-run with:"
  echo ">>            ALARM_EMAIL=ops@example.com deploy/cloud/deploy.sh azure $DOMAIN"
fi

# ---- Preflight 1: resource providers ---------------------------------------------------------
# A fresh subscription has these UNREGISTERED, and the failures do not say so clearly:
#   * Microsoft.App           -> 409 MissingSubscriptionRegistration (at least names the problem)
#   * Microsoft.DBforPostgreSQL -> 400 "The value of the 'Version' should be in: []"
#
# That second one is the trap. An EMPTY list of valid versions reads as "PostgreSQL 16 is not
# supported here", sending an operator off to change `version` or the region — when the real
# cause is that the subscription cannot enumerate the provider's capabilities at all. Both were
# hit on a real deploy. Registration is idempotent and near-instant when already done.
echo ">> ensuring Azure resource providers are registered..."
for _ns in Microsoft.App Microsoft.DBforPostgreSQL Microsoft.Cache \
           Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.Network; do
  _state="$(az provider show --namespace "$_ns" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
  if [ "$_state" != "Registered" ]; then
    echo "   registering $_ns (currently $_state)..."
    az provider register --namespace "$_ns" --wait
  fi
done
echo ">> resource providers ready."

# ---- Preflight 2: the Terraform state container ------------------------------------------------
# versions.tf pins an azurerm backend. `terraform init` fails with a bare
# "404 The specified container does not exist" if the blob container was never created — which is
# easy to miss, because the storage ACCOUNT exists and the error names neither the account nor
# the fix. Created here with the account key rather than --auth-mode login on purpose: being
# Contributor on the subscription does NOT grant blob DATA access (that needs Storage Blob Data
# Contributor), so the AAD path fails for most operators who can otherwise deploy everything else.
_sa="$(grep -oP 'storage_account_name\s*=\s*"\K[^"]+' versions.tf 2>/dev/null || true)"
_sc="$(grep -oP 'container_name\s*=\s*"\K[^"]+' versions.tf 2>/dev/null || true)"
_srg="$(grep -oP 'resource_group_name\s*=\s*"\K[^"]+' versions.tf 2>/dev/null || true)"
if [ -n "$_sa" ] && [ -n "$_sc" ] && [ -n "$_srg" ]; then
  if ! az storage container show --name "$_sc" --account-name "$_sa" \
        --account-key "$(az storage account keys list --account-name "$_sa" \
          --resource-group "$_srg" --query '[0].value' -o tsv 2>/dev/null)" >/dev/null 2>&1; then
    echo ">> creating Terraform state container '$_sc' in '$_sa'..."
    az storage container create --name "$_sc" --account-name "$_sa" \
      --account-key "$(az storage account keys list --account-name "$_sa" \
        --resource-group "$_srg" --query '[0].value' -o tsv)" >/dev/null
  fi
fi

terraform init -input=false
if [ "$ACTION" = "destroy" ]; then terraform destroy "${TF[@]}" -auto-approve; exit 0; fi

echo ">> creating ACR..."
terraform apply "${TF[@]}" -target=azurerm_container_registry.main -auto-approve -input=false
REG="$(terraform output -raw acr_login_server)" # e.g. nexusprodacr.azurecr.io

# Build ON ACR, not on this machine. `az acr build` uploads the build context and runs the
# Dockerfile in ACR Tasks, which matters for three reasons:
#
#   * AZURE CLOUD SHELL HAS NO DOCKER DAEMON. `docker build` fails there outright, and Cloud
#     Shell is the zero-install way to run this script — the alternative is asking every operator
#     to install Docker locally just to ship a deploy.
#   * It is the same mechanism azure-pipelines-ci.yml uses, so an operator-run deploy and a
#     CI-run deploy produce the image the same way rather than by two different paths.
#   * No local pull of the python:3.11-slim base layer, and no push of the built layers back up
#     — on a slow link that is the difference between a 5-minute and a 25-minute deploy.
#
# The AWS path above still uses docker build+push; ECR has no server-side build equivalent.
echo ">> build on ACR: $REG/$PROJECT:$TAG"
az acr build \
  --registry "${REG%%.*}" \
  --image "$PROJECT:$TAG" \
  --image "$PROJECT:latest" \
  --file "$REPO_ROOT/deploy/Dockerfile" \
  "$REPO_ROOT"
echo ">> full apply..."
terraform apply "${TF[@]}" -var "image=$REG/$PROJECT:$TAG" -auto-approve -input=false
echo; echo "Done. App (default ingress): $(terraform output -raw app_default_url)"
echo ">> Custom domain: $(terraform output -raw custom_domain_next_step)"
