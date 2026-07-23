#!/usr/bin/env bash
# O-1 — Postgres logical backup (custom format, compressed, restorable with pg_restore).
#
#   scripts/backup_db.sh [output_dir]
#
# Targets the docker-compose Postgres service by default; override the connection with env vars
# to back up a managed/remote DB. Writes backups/nexus_<UTC-timestamp>.dump and prints its path.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
PG_SERVICE="${PG_SERVICE:-db}"
PGUSER="${PGUSER:-nexus}"
PGDATABASE="${PGDATABASE:-nexus}"
OUT_DIR="${1:-backups}"

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUT_DIR/nexus_${STAMP}.dump"

echo "[backup] pg_dump ${PGDATABASE} (custom format) -> ${OUT}"
# -Fc = custom (compressed, selective restore); -T pipes without a TTY.
$COMPOSE exec -T "$PG_SERVICE" pg_dump -U "$PGUSER" -Fc "$PGDATABASE" > "$OUT"

SIZE="$(wc -c < "$OUT" | tr -d ' ')"
echo "[backup] done: ${OUT} (${SIZE} bytes)"
echo "$OUT"
