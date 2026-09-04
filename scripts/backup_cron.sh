#!/usr/bin/env bash
# O-1 — scheduled backup wrapper for cron. Backs up, prunes old dumps (keep last N), and (if an
# offsite target is configured) copies the fresh dump off the box — because a backup that lives
# only on the VM dies with the VM.
#
# Install (daily at 02:15, from the repo root):
#   crontab -e
#   15 2 * * * cd /opt/nexus-gtm && BACKUP_OFFSITE="s3://my-bucket/nexus" scripts/backup_cron.sh >> /var/log/nexus-backup.log 2>&1
#
# Env:
#   COMPOSE          (default: docker compose -f deploy/docker-compose.prod.yml)
#   BACKUP_DIR       (default: backups)        local dump directory
#   BACKUP_KEEP      (default: 7)              how many recent dumps to retain locally
#   BACKUP_OFFSITE   (optional)                e.g. s3://bucket/prefix — aws s3 cp is used if set
#
# `COMPOSE` defaults to the production compose FILE, not a bare `docker compose`. Cron runs from
# the repo root where a bare invocation finds no compose file, so the default that reads as
# harmless was a second reason a scheduled backup could never have worked.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.prod.yml}"
export COMPOSE
BACKUP_DIR="${BACKUP_DIR:-backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
STATUS_FILE="${BACKUP_DIR}/LAST_RUN"

mkdir -p "$BACKUP_DIR"

# A silent backup failure is the whole hazard: nobody looks at a backup log until they need a
# restore, and by then the gap is however long it has been broken. Record every outcome where a
# monitoring check can read it, and make failure the loud case.
record() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" > "$STATUS_FILE"; }
on_error() { record "FAILED"; echo "[cron] BACKUP FAILED — no dump written this run" >&2; }
trap on_error ERR

echo "[cron $(date -u +%FT%TZ)] starting backup"
DUMP="$(bash scripts/backup_db.sh "$BACKUP_DIR" | tail -1)"

if [ ! -s "$DUMP" ]; then
  echo "[cron] BACKUP FAILED — reported dump is missing or empty: $DUMP" >&2
  record "FAILED"
  exit 1
fi

# Offsite copy (best-effort; a failed upload must not delete the local dump).
OFFSITE="skipped"
if [ -n "${BACKUP_OFFSITE:-}" ]; then
  echo "[cron] copying $DUMP -> $BACKUP_OFFSITE/"
  if command -v aws >/dev/null 2>&1; then
    if aws s3 cp "$DUMP" "$BACKUP_OFFSITE/"; then OFFSITE="ok"; else
      OFFSITE="FAILED"; echo "[cron] WARN offsite upload failed" >&2
    fi
  else
    OFFSITE="no-aws-cli"; echo "[cron] WARN aws CLI not found; skipping offsite copy" >&2
  fi
fi

# Retention: keep the newest N local dumps, delete the rest. Only reached once a verified dump
# exists this run, so a broken backup can never prune the last good one it failed to replace.
echo "[cron] pruning local dumps, keeping newest ${BACKUP_KEEP}"
ls -1t "$BACKUP_DIR"/nexus_*.dump 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do
  echo "[cron] removing old dump: $old"
  rm -f "$old"
done

record "OK offsite=${OFFSITE} dump=$(basename "$DUMP")"
echo "[cron $(date -u +%FT%TZ)] backup complete"
