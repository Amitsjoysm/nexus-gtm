#!/bin/sh
# Container entrypoint: bring the database schema up to head before serving, so a plain
# `docker compose up` (not just deploy.sh) can never run app code against a stale schema.
# Only the app runs migrations (NEXUS_RUN_MIGRATIONS=1); the worker skips this and just starts.
set -e

if [ "${NEXUS_RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] bootstrapping database (create-or-migrate)..."
  python scripts/bootstrap_db.py
  echo "[entrypoint] database ready."
fi

exec "$@"
