#!/usr/bin/env bash
# NEXUS GTM — one-script production deploy for a single VM.
#
#   ./deploy.sh app.autosdr.ai          first deploy (or domain change)
#   ./deploy.sh                          redeploy current version (uses existing .env)
#
# Requires: docker with the compose plugin. Everything else (TLS certs, secrets,
# schema migrations, service startup, health check) is handled here.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN_ARG="${1:-}"
ENV_FILE=".env"

say() { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is required (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

# ---- .env bootstrap -----------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  say "no .env found — creating from .env.production.example"
  cp .env.production.example "$ENV_FILE"
fi

# Generated secrets on first run (never committed; .env is gitignored).
gen() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
if grep -q '^POSTGRES_PASSWORD=CHANGE_ME' "$ENV_FILE"; then
  say "generating POSTGRES_PASSWORD"
  sed -i.bak "s/^POSTGRES_PASSWORD=CHANGE_ME/POSTGRES_PASSWORD=$(gen)/" "$ENV_FILE"
fi
if grep -q '^NEXUS_SECRET_KEY=CHANGE_ME' "$ENV_FILE"; then
  say "generating NEXUS_SECRET_KEY"
  sed -i.bak "s/^NEXUS_SECRET_KEY=CHANGE_ME/NEXUS_SECRET_KEY=$(gen)/" "$ENV_FILE"
fi
rm -f "$ENV_FILE.bak"

# Domain: positional arg wins, otherwise keep what .env has.
if [[ -n "$DOMAIN_ARG" ]]; then
  say "setting DOMAIN=$DOMAIN_ARG"
  sed -i.bak "s/^DOMAIN=.*/DOMAIN=$DOMAIN_ARG/" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
fi
DOMAIN="$(grep '^DOMAIN=' "$ENV_FILE" | cut -d= -f2)"
[[ -n "$DOMAIN" ]] || die "DOMAIN is not set in $ENV_FILE"

# ---- Build, migrate, start ------------------------------------------------------
say "building image (frontend + backend)"
docker compose -f docker-compose.prod.yml build app

say "starting postgres + valkey"
docker compose -f docker-compose.prod.yml up -d postgres valkey

say "bootstrapping database (create-or-migrate)"
docker compose -f docker-compose.prod.yml run --rm app python scripts/bootstrap_db.py

say "starting app, worker, caddy"
docker compose -f docker-compose.prod.yml up -d

# ---- Health gate ----------------------------------------------------------------
say "waiting for the app to report healthy"
for i in $(seq 1 30); do
  if docker compose -f docker-compose.prod.yml exec -T app \
       python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/ready', timeout=3).status==200 else 1)" \
       >/dev/null 2>&1; then
    say "READY — https://${DOMAIN}  (sign up to create the first workspace)"
    say "logs:    docker compose -f docker-compose.prod.yml logs -f app worker"
    say "backup:  docker compose -f docker-compose.prod.yml exec postgres pg_dump -U nexus nexus > backup.sql"
    exit 0
  fi
  sleep 2
done
die "app did not become ready; check: docker compose -f docker-compose.prod.yml logs app"
