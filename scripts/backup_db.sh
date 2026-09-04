#!/usr/bin/env bash
# O-1 — Postgres logical backup (custom format, compressed, restorable with pg_restore).
#
#   scripts/backup_db.sh [output_dir]
#
# Targets the docker-compose Postgres service by default; override the connection with env vars
# to back up a managed/remote DB. Writes backups/nexus_<UTC-timestamp>.dump and prints its path.
#
# A backup is only a backup if it can be restored, so this script REFUSES to leave a file behind
# that it has not verified. Two failures made that necessary, both found by running it:
#
#   1. PG_SERVICE defaulted to `db`; the compose service is `postgres`. Every scheduled backup
#      would have failed from the day it was installed.
#   2. The dump was written with a shell redirect, which creates the file BEFORE pg_dump runs. So
#      the failure left `nexus_<timestamp>.dump` at 0 bytes — a correct-looking name that `ls`
#      cannot tell from a real backup, and that retention then counted as one of the 7 kept.
#
# So: dump to a .partial, verify it lists as an archive, and only then move it into place. A
# missing backup is a visible problem; a backup-shaped file that cannot be restored is not.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PGUSER="${PGUSER:-nexus}"
PGDATABASE="${PGDATABASE:-nexus}"
OUT_DIR="${1:-backups}"

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUT_DIR/nexus_${STAMP}.dump"
PARTIAL="${OUT}.partial"

# Never leave a partial behind for a later run to mistake for a dump.
cleanup() { rm -f "$PARTIAL"; }
trap cleanup EXIT

echo "[backup] pg_dump ${PGDATABASE} (custom format) -> ${OUT}"
# -Fc = custom (compressed, selective restore); -T pipes without a TTY.
if ! $COMPOSE exec -T "$PG_SERVICE" pg_dump -U "$PGUSER" -Fc "$PGDATABASE" > "$PARTIAL"; then
  echo "[backup] FAILED: pg_dump did not complete. No dump written." >&2
  exit 1
fi

SIZE="$(wc -c < "$PARTIAL" | tr -d ' ')"
if [ "$SIZE" -lt 1000 ]; then
  echo "[backup] FAILED: dump is ${SIZE} bytes — too small to be a real database. Discarded." >&2
  exit 1
fi

# Prove it is a readable archive before calling it a backup. `pg_restore --list` parses the table
# of contents without touching a database, so this is safe to run anywhere and catches truncation,
# a partial write, and the 0-byte case above.
if ! $COMPOSE exec -T "$PG_SERVICE" pg_restore --list < "$PARTIAL" > /dev/null 2>&1; then
  echo "[backup] FAILED: dump is not a readable pg_restore archive. Discarded." >&2
  exit 1
fi

mv "$PARTIAL" "$OUT"
echo "[backup] done: ${OUT} (${SIZE} bytes, archive verified)"
echo "$OUT"
