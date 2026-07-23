#!/usr/bin/env bash
# O-1 — restore a Postgres logical backup produced by backup_db.sh.
#
#   scripts/restore_db.sh <dumpfile>
#
# Drops and recreates the target database, then pg_restore's the dump. DESTRUCTIVE — the current
# contents of the target DB are replaced. Refuses to run without an explicit dump path.
set -euo pipefail

DUMP="${1:?usage: restore_db.sh <dumpfile>}"
[ -f "$DUMP" ] || { echo "[restore] no such file: $DUMP" >&2; exit 1; }

COMPOSE="${COMPOSE:-docker compose}"
PG_SERVICE="${PG_SERVICE:-db}"
PGUSER="${PGUSER:-nexus}"
PGDATABASE="${PGDATABASE:-nexus}"

echo "[restore] terminating connections + recreating ${PGDATABASE}"
$COMPOSE exec -T "$PG_SERVICE" psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = '${PGDATABASE}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${PGDATABASE};
CREATE DATABASE ${PGDATABASE} OWNER ${PGUSER};
SQL

echo "[restore] pg_restore ${DUMP} -> ${PGDATABASE}"
# --clean --if-exists is redundant after the drop, but keeps the restore idempotent.
$COMPOSE exec -T "$PG_SERVICE" pg_restore -U "$PGUSER" -d "$PGDATABASE" --no-owner < "$DUMP"

echo "[restore] done"
