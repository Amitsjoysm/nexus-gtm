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
#   BACKUP_DIR       (default: backups)        local dump directory
#   BACKUP_KEEP      (default: 7)              how many recent dumps to retain locally
#   BACKUP_OFFSITE   (optional)               e.g. s3://bucket/prefix — aws s3 cp is used if set
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"

echo "[cron $(date -u +%FT%TZ)] starting backup"
DUMP="$(bash scripts/backup_db.sh "$BACKUP_DIR" | tail -1)"

# Offsite copy (best-effort; a failed upload must not delete the local dump).
if [ -n "${BACKUP_OFFSITE:-}" ]; then
  echo "[cron] copying $DUMP -> $BACKUP_OFFSITE/"
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$DUMP" "$BACKUP_OFFSITE/" || echo "[cron] WARN offsite upload failed" >&2
  else
    echo "[cron] WARN aws CLI not found; skipping offsite copy" >&2
  fi
fi

# Retention: keep the newest N local dumps, delete the rest.
echo "[cron] pruning local dumps, keeping newest ${BACKUP_KEEP}"
ls -1t "$BACKUP_DIR"/nexus_*.dump 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do
  echo "[cron] removing old dump: $old"
  rm -f "$old"
done

echo "[cron $(date -u +%FT%TZ)] backup complete"
