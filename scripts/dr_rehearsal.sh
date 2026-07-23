#!/usr/bin/env bash
# O-1 — Disaster-Recovery rehearsal. Proves backup + restore actually work, end to end, and
# reports a measured RTO (restore wall-clock) and RPO (data written after the backup that a
# restore would lose). Run against the local docker stack:
#
#   scripts/dr_rehearsal.sh
#
# Steps:
#   1. Write a known marker row (the "last committed transaction before backup").
#   2. Back up.
#   3. Write a SECOND marker AFTER the backup (this is what an RPO gap would lose).
#   4. Simulate data loss (drop the marker table).
#   5. Restore from the backup, timing it (RTO).
#   6. Verify the pre-backup marker is present and the post-backup marker is absent
#      (i.e. RPO = data written since the last backup, as expected for point-in-time restore).
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
PG_SERVICE="${PG_SERVICE:-db}"
PGUSER="${PGUSER:-nexus}"
PGDATABASE="${PGDATABASE:-nexus}"

psql() { $COMPOSE exec -T "$PG_SERVICE" psql -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1 -tAc "$1"; }

echo "=== DR REHEARSAL ==="
echo "[1/6] writing pre-backup marker"
psql "CREATE TABLE IF NOT EXISTS _dr_marker (id serial primary key, label text, at timestamptz default now());" >/dev/null
psql "INSERT INTO _dr_marker (label) VALUES ('pre-backup');" >/dev/null
PRE_COUNT="$(psql "SELECT count(*) FROM _dr_marker WHERE label='pre-backup';")"

echo "[2/6] backing up"
DUMP="$(COMPOSE="$COMPOSE" PG_SERVICE="$PG_SERVICE" PGUSER="$PGUSER" PGDATABASE="$PGDATABASE" bash scripts/backup_db.sh backups | tail -1)"

echo "[3/6] writing post-backup marker (this is the RPO gap a restore will lose)"
psql "INSERT INTO _dr_marker (label) VALUES ('post-backup');" >/dev/null

echo "[4/6] simulating data loss (DROP TABLE _dr_marker)"
psql "DROP TABLE _dr_marker;" >/dev/null

echo "[5/6] restoring from backup (timing RTO)"
START=$(date +%s)
COMPOSE="$COMPOSE" PG_SERVICE="$PG_SERVICE" PGUSER="$PGUSER" PGDATABASE="$PGDATABASE" bash scripts/restore_db.sh "$DUMP" >/dev/null
END=$(date +%s)
RTO=$((END - START))

echo "[6/6] verifying"
PRE_AFTER="$(psql "SELECT count(*) FROM _dr_marker WHERE label='pre-backup';")"
POST_AFTER="$(psql "SELECT count(*) FROM _dr_marker WHERE label='post-backup';")"

echo
echo "=== RESULT ==="
echo "  RTO (restore wall-clock):        ${RTO}s"
echo "  pre-backup marker restored:      ${PRE_AFTER} (expected ${PRE_COUNT})"
echo "  post-backup marker after restore: ${POST_AFTER} (expected 0 — the RPO gap)"

# Cleanup the rehearsal artifact.
psql "DROP TABLE IF EXISTS _dr_marker;" >/dev/null || true

if [ "$PRE_AFTER" = "$PRE_COUNT" ] && [ "$POST_AFTER" = "0" ]; then
  echo "  VERDICT: PASS — backup restores cleanly; RPO = writes since last backup, as designed."
  exit 0
else
  echo "  VERDICT: FAIL — restore did not reproduce the expected state." >&2
  exit 1
fi
