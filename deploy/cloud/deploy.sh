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
PROJECT="${PROJECT:-nexus}"
REGION="${AWS_REGION:-us-east-1}"
LOCATION="${AZURE_LOCATION:-eastus2}"
ENV_NAME="${ENV_NAME:-prod}"

# EACH ENVIRONMENT GETS ITS OWN SECRETS, and this is not a preference.
#
# This was a single hardcoded `deploy/.env`, so a staging deploy inherited PRODUCTION's secrets
# wholesale. The JWT signing key is the obvious objection — a token minted by staging verifies in
# production — but the expensive one is the API keys:
#
#   * Staging test runs burn production's Groq/Exa quota, and a rate limit hit in staging is a
#     rate limit hit for real customers.
#   * A live Stripe key in that file means a staging test can CHARGE A REAL CARD. Money fails
#     silently; nothing in the product would report it.
#   * The database passwords are shared across two servers, so a leak from the lower-trust
#     environment is a leak of production's credentials.
#
# Resolution order, most specific first:
#   1. $ENV_FILE, if explicitly set          — full manual control
#   2. deploy/.env.<env>, if it exists       — the safety net: it cannot be forgotten
#   3. deploy/.env                           — unchanged behaviour for single-environment setups
#
# Step 2 is deliberately automatic rather than opt-in. An opt-in flag is one a tired operator
# forgets exactly once, and the failure is invisible: staging comes up perfectly, having quietly
# armed itself with production's credentials.
if [ -n "${ENV_FILE:-}" ]; then
  :
elif [ -f "$REPO_ROOT/deploy/.env.$ENV_NAME" ]; then
  ENV_FILE="$REPO_ROOT/deploy/.env.$ENV_NAME"
  echo ">> using per-environment secrets: deploy/.env.$ENV_NAME"
else
  ENV_FILE="$REPO_ROOT/deploy/.env"
  # Warn, do not fail: a single-environment deployment legitimately has only deploy/.env, and
  # refusing it would break every existing user of this script for a risk they do not have.
  if [ "$ENV_NAME" != "prod" ]; then
    echo ">> WARNING: no deploy/.env.$ENV_NAME — falling back to deploy/.env."
    echo ">>          '$ENV_NAME' will run with PRODUCTION's secrets: same JWT signing key,"
    echo ">>          same database passwords, and the SAME API KEYS (including any payment"
    echo ">>          provider key). Create deploy/.env.$ENV_NAME before deploying anything"
    echo ">>          that sends email, calls a paid API, or takes money."
  fi
fi

# THE IMAGE REPOSITORY IS NOT THE PROJECT NAME, and conflating them cost a production revert.
#
# `PROJECT` names Azure RESOURCES (nexus-prod-rg, nexus-prod-app, the ACR). The image repository
# inside that registry is a separate identifier, and both Azure Pipelines files have always
# called it `nexus-gtm` (azure-pipelines-ci.yml IMAGE_REPO). This script used $PROJECT for both,
# so it pushed `nexus:<tag>` while CI pushed `nexus-gtm:<sha>` — two repositories in one registry
# holding the same application.
#
# That is not merely untidy. CD updates the running app out of band with `az containerapp update`,
# so Terraform's recorded `image` stays at whatever THIS script last bootstrapped. The next
# `terraform apply` — a routine scaling change, an alert-email edit — then reverts production to
# a months-old bootstrap image. Silently, and reported as a successful apply.
#
# Renaming PROJECT instead would rename every Azure resource, which is a rebuild. So the image
# repo becomes its own variable, defaulting to what the pipelines already expect.
IMAGE_REPO="${IMAGE_REPO:-nexus-gtm}"

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
TF=(-var "project=$PROJECT" -var "env=$ENV_NAME" -var "location=$LOCATION" -var "domain=$DOMAIN")

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

# The backend key is per-environment (versions.tf is a PARTIAL backend — see the comment there).
# `-reconfigure` is required because the key changes between a staging and a production run in the
# same checkout: without it Terraform reuses the key cached in .terraform/ from the previous run
# and would apply staging's config against production's state.
STATE_KEY="${STATE_KEY:-$PROJECT/$ENV_NAME.tfstate}"
echo ">> terraform init (state key: $STATE_KEY)"
terraform init -input=false -reconfigure -backend-config="key=$STATE_KEY"
if [ "$ACTION" = "destroy" ]; then terraform destroy "${TF[@]}" -auto-approve; exit 0; fi

# ---- The registry: create it, or consume the one production already owns ----------------------
# Staging must push to PRODUCTION'S registry, because azure-pipelines-cd.yml promotes a release by
# deploying the same image digest to both. See the comment on azurerm_container_registry.main.
#
#   ACR_SHARED_NAME= (unset)  -> this environment creates and owns a registry  (production)
#   ACR_SHARED_NAME=<name>    -> look up and push to that one                  (staging)
if [ -n "${ACR_SHARED_NAME:-}" ]; then
  [ -n "${ACR_SHARED_RG:-}" ] || { echo "ACR_SHARED_NAME is set but ACR_SHARED_RG is not. Both are required."; exit 1; }
  TF+=(-var "acr_shared_name=$ACR_SHARED_NAME" -var "acr_shared_resource_group=$ACR_SHARED_RG")
  # Derived, not read from `terraform output`: on a first staging deploy nothing has been applied
  # yet, so the output does not exist. The login server for an ACR is always <name>.azurecr.io.
  REG="${ACR_SHARED_NAME}.azurecr.io"
  echo ">> using shared ACR: $REG (resource group $ACR_SHARED_RG)"
else
  echo ">> creating ACR..."
  # `[0]` because the resource is now count-gated. Targeting the bare address fails with
  # "Resource not found in configuration" — which reads like the resource was deleted.
  terraform apply "${TF[@]}" -target='azurerm_container_registry.main[0]' -auto-approve -input=false

  # A -TARGETED APPLY DOES NOT RELIABLY WRITE OUTPUTS, and this one does not.
  #
  # `output.acr_login_server` reads `local.acr`, which is a conditional over BOTH
  # `azurerm_container_registry.main[0]` and the shared-registry DATA SOURCE. Under `-target`
  # Terraform prunes the graph to the targeted node and its dependencies; the data source is
  # outside that set, so the local is never evaluated and the output never reaches state.
  #
  # `terraform output -raw` then prints an EMPTY STRING and exits non-zero on stderr only, so
  # the old unguarded assignment produced `az acr build --registry ""` — which fails with
  # "Registry names may contain only alpha numeric characters and must be between 5 and 50
  # characters", a complaint about a name nobody typed, pointing at the registry rather than at
  # the empty variable. Measured on a real first deploy 2026-08-28.
  #
  # Asking Azure is authoritative and independent of Terraform's targeting semantics: at this
  # point the registry demonstrably exists, because the apply above just created it.
  REG="$(terraform output -raw acr_login_server 2>/dev/null || true)"
  if [ -z "$REG" ]; then
    echo ">> output not populated by the targeted apply; asking Azure directly..."
    REG="$(az acr list --resource-group "${PROJECT}-${ENV_NAME}-rg" --query '[0].loginServer' -o tsv 2>/dev/null || true)"
  fi
  # Never let an empty value reach `az acr build`. The failure is confusing enough with a name;
  # without one it sends the operator to check the registry, which is not the problem.
  [ -n "$REG" ] || { echo "ERROR: could not resolve the ACR login server in ${PROJECT}-${ENV_NAME}-rg"; exit 1; }
fi

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
echo ">> build on ACR: $REG/$IMAGE_REPO:$TAG"
az acr build \
  --registry "${REG%%.*}" \
  --image "$IMAGE_REPO:$TAG" \
  --image "$IMAGE_REPO:latest" \
  --file "$REPO_ROOT/deploy/Dockerfile" \
  "$REPO_ROOT"
echo ">> full apply..."
terraform apply "${TF[@]}" -var "image=$REG/$IMAGE_REPO:$TAG" -auto-approve -input=false
echo; echo "Done. App (default ingress): $(terraform output -raw app_default_url)"
echo ">> Custom domain: $(terraform output -raw custom_domain_next_step)"
