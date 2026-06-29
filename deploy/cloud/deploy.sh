#!/usr/bin/env bash
# Single-script cloud deploy for Infojoy GTM.
#
#   deploy/cloud/deploy.sh aws   app.example.com            # AWS ECS Fargate (self-hosted data)
#   USE_MANAGED_DATA=true \
#   deploy/cloud/deploy.sh aws   app.example.com            # AWS + RDS Multi-AZ + ElastiCache  (variant a)
#   deploy/cloud/deploy.sh azure app.example.com            # Azure Container Apps + managed data (variant b)
#   deploy/cloud/deploy.sh <cloud> <domain> destroy         # tear down
#
# Prereqs: terraform >= 1.6, docker, and the cloud CLI (aws / az) authenticated. Secrets come from
# deploy/.env (gitignored); Terraform composes the DB/Redis URLs from the real endpoints.
set -euo pipefail

CLOUD="${1:-}"
DOMAIN="${2:-}"
ACTION="${3:-apply}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$REPO_ROOT/deploy/.env"
PROJECT="${PROJECT:-nexus}"
REGION="${AWS_REGION:-us-east-1}"
LOCATION="${AZURE_LOCATION:-eastus}"

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
out = {k: v for k, v in env.items() if k not in skip and v}
out.setdefault("POSTGRES_PASSWORD", pysecrets.token_hex(24))
out.setdefault("NEXUS_APP_DB_PASSWORD", pysecrets.token_hex(24))
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
terraform init -input=false
if [ "$ACTION" = "destroy" ]; then terraform destroy "${TF[@]}" -auto-approve; exit 0; fi

echo ">> creating ACR..."
terraform apply "${TF[@]}" -target=azurerm_container_registry.main -auto-approve -input=false
REG="$(terraform output -raw acr_login_server)" # e.g. nexusprodacr.azurecr.io
echo ">> build + push $REG/$PROJECT:$TAG"
az acr login --name "${REG%%.*}"
build_and_push "$REG/$PROJECT:$TAG"
echo ">> full apply..."
terraform apply "${TF[@]}" -var "image=$REG/$PROJECT:$TAG" -auto-approve -input=false
echo; echo "Done. App (default ingress): $(terraform output -raw app_default_url)"
echo ">> Custom domain: $(terraform output -raw custom_domain_next_step)"
