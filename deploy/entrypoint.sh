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
  # Wait for the database to accept TCP connections before migrating. On orchestrators without
  # cross-service depends_on (e.g. ECS), the app task can start before Postgres is ready; without
  # this the migration fails and the task crash-loops until Postgres happens to be up. No-op for
  # SQLite (no host). Dependency-free (stdlib socket).
  echo "[entrypoint] waiting for the database to accept connections..."
  NEXUS_DB_WAIT_URL="$OWNER_URL" python - <<'PY'
import os, socket, sys, time
from urllib.parse import urlparse
u = urlparse(os.environ.get("NEXUS_DB_WAIT_URL", "").replace("+asyncpg", "").replace("+psycopg", ""))
host, port = u.hostname, (u.port or 5432)
if not host:
    sys.exit(0)  # sqlite / no host — nothing to wait for
deadline = time.time() + 120
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"[entrypoint] database {host}:{port} is up"); sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"[entrypoint] WARNING: {host}:{port} not reachable after 120s; continuing")
PY
  echo "[entrypoint] bootstrapping database (create-or-migrate)..."
  NEXUS_DATABASE_URL="$OWNER_URL" python scripts/bootstrap_db.py
  echo "[entrypoint] applying tenant-isolation hardening (least-privilege role + RLS)..."
  NEXUS_DATABASE_URL="$OWNER_URL" python scripts/apply_rls.py
  echo "[entrypoint] database ready."
fi

# Prometheus multiprocess mode: uvicorn runs 2 workers, each with its own registry, so without a
# shared directory a scrape hits one at random and reports roughly half the traffic. The files are
# per-process and stale ones are never reclaimed, so the directory MUST be emptied on start —
# otherwise counters from workers that died in a previous container keep being summed in forever.
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db 2>/dev/null || true
  echo "[entrypoint] prometheus multiprocess dir ready at $PROMETHEUS_MULTIPROC_DIR"
fi

exec "$@"
