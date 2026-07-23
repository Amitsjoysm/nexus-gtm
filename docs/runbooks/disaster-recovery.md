# Runbook — Disaster Recovery (O-1)

Backup, restore, and a rehearsal that proves both actually work and measures RTO/RPO.

**Owner:** Ops / on-call. **Scripts:** `scripts/backup_db.sh`, `scripts/restore_db.sh`,
`scripts/dr_rehearsal.sh`. All target the docker-compose Postgres service (`db`) by default; override
`COMPOSE`, `PG_SERVICE`, `PGUSER`, `PGDATABASE` to point at a managed/remote DB.

## RPO / RTO

- **RPO (Recovery Point Objective) = time since the last backup.** A logical `pg_dump` is a
  point-in-time snapshot; anything written after it is lost on restore. Schedule backups to match
  your tolerated data-loss window (e.g. hourly `cron`). For near-zero RPO, add continuous WAL
  archiving / PITR (Postgres `archive_command` + base backups) — out of scope for the single-VM
  stack but the natural next step.
- **RTO (Recovery Time Objective) = restore wall-clock.** Measured by the rehearsal below.
  **Local rehearsal result: RTO ≈ 7 s** for a small database; scales with data size.

## Routine backup

```bash
scripts/backup_db.sh                 # writes backups/nexus_<UTC-timestamp>.dump
# schedule it (host cron), and copy the dump OFF the box (S3/blob) — a backup on the same VM that
# dies with the VM is not a backup.
```

## Restore (recover from a dump)

```bash
scripts/restore_db.sh backups/nexus_<timestamp>.dump    # DESTRUCTIVE: replaces the target DB
docker compose restart api worker                        # reconnect the app to the fresh DB
```

The app reconnects automatically (SQLAlchemy `pool_pre_ping` + `restart: unless-stopped`), so a
restore under a running stack self-heals once connections re-establish.

## Rehearsal (prove it end-to-end)

```bash
scripts/dr_rehearsal.sh
```

It writes a pre-backup marker, backs up, writes a post-backup marker, drops the table (simulated
loss), restores while timing the RTO, and asserts the pre-backup marker returns and the
post-backup marker does not (i.e. the RPO gap is exactly the writes since the last backup).
**Verified PASS locally** (RTO 7 s; pre-backup restored; post-backup correctly absent).

Run the rehearsal on a schedule (e.g. monthly) against staging — an untested backup is a hope,
not a recovery plan.

## Escalation

If a restore fails: confirm the dump is non-empty and readable (`pg_restore --list <dump>`), check
Postgres logs (`docker compose logs db`), and fall back to the previous good dump. Keep at least 7
daily dumps offsite so a corrupt latest backup is never the only option.
