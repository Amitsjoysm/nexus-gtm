#!/usr/bin/env bash
# One-time Codespaces bootstrap: materialize .env, pull the Docker Hub images, start the stack,
# and seed the demo tenant. Safe to re-run. Never fails the codespace on a soft error.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.hub.yml"

# 1) .env — prefer the Codespaces secret DOTENV (the full .env contents, kept OUT of git). If it's
#    not set, fall back to the committed template so the app still boots (in stub/degraded mode).
if [ -n "${DOTENV:-}" ]; then
  printf '%s\n' "$DOTENV" > .env
  echo "[setup] .env written from Codespaces secret DOTENV ($(wc -l < .env) lines)"
elif [ ! -f .env ]; then
  cp .env.example .env
  echo "[setup] .env created from .env.example — set a DOTENV Codespaces secret for full features"
else
  echo "[setup] using existing .env"
fi

# 2) Pull the published images and start the stack.
echo "[setup] pulling images from Docker Hub…"
$COMPOSE pull
echo "[setup] starting stack…"
$COMPOSE up -d

# 3) Wait for the API health check, then seed the demo tenant (idempotent; run inside the api
#    container which already has httpx + the script — no host Python deps needed).
echo "[setup] waiting for the API to become healthy…"
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "[setup] API is up"
    break
  fi
  sleep 3
done

echo "[setup] seeding demo data…"
if $COMPOSE exec -T api python scripts/seed_demo.py --base-url http://localhost:8000; then
  echo "[setup] seed complete"
else
  echo "[setup] seed skipped (already seeded or API still starting) — re-run later with:"
  echo "        $COMPOSE exec api python scripts/seed_demo.py --base-url http://localhost:8000"
fi

echo "[setup] done → open the forwarded port 8000 (login: owner@northwind.example / demo-password-123 / workspace 'northwind')"
