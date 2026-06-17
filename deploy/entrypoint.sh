#!/bin/sh
# Container entrypoint: bring the database schema up to head before serving, so a plain
# `docker compose up` (not just deploy.sh) can never run app code against a stale schema.
# Only the app runs this (NEXUS_RUN_MIGRATIONS=1); the worker skips it and just starts.
#
# Migrations + role/RLS provisioning need owner privileges (DDL, CREATE ROLE), so they run
# against NEXUS_DB_OWNER_URL when set. The app process itself (exec below) then serves as the
# least-privilege role in the container's NEXUS_DATABASE_URL.
set -e

if [ "${NEXUS_RUN_MIGRATIONS:-0}" = "1" ]; then
  OWNER_URL="${NEXUS_DB_OWNER_URL:-$NEXUS_DATABASE_URL}"
  echo "[entrypoint] bootstrapping database (create-or-migrate)..."
  NEXUS_DATABASE_URL="$OWNER_URL" python scripts/bootstrap_db.py
  echo "[entrypoint] applying tenant-isolation hardening (least-privilege role + RLS)..."
  NEXUS_DATABASE_URL="$OWNER_URL" python scripts/apply_rls.py
  echo "[entrypoint] database ready."
fi

exec "$@"
