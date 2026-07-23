# Go-Live Checklist — Pilot (100–250 users, single region)

Everything below is implemented and verified against the live Docker stack. Follow this to deploy.

## What was hardened (all verified live)

| Item | Status | Evidence |
|---|---|---|
| Auth rate limiting (brute force) | ✅ ON | 26 rapid logins → 11× `429` |
| Idempotency (duplicate POSTs) | ✅ ON | duplicate `Idempotency-Key` → `idempotent-replay: true`, account created once |
| Request body-size cap (DoS) | ✅ ON (10 MB) | 11 MB body → `413` |
| Security headers | ✅ ON | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` present |
| `/metrics` public exposure | ✅ Blocked at Caddy | `respond @metrics 404` in Caddyfile |
| Prometheus + Grafana + alerts | ✅ Live | target `up`, 4 rules, dashboard provisioned |
| Alertmanager | ✅ Wired | Prometheus → 1 active alertmanager |
| Backups + DR | ✅ Proven | rehearsal PASS, RTO ≈ 7 s |
| Chaos resilience | ✅ Verified | DB down → `/ready` 503 + auto-recover; queue down → worker survives; 2 workers → no races |

## Deploy steps (single VM)

1. **Provision** a 2–4 vCPU / 8 GB VM with Docker. Point your DNS `A` record at it.
2. **Secrets & config:**
   ```bash
   cd deploy
   cp .env.production.example .env
   # deploy.sh generates POSTGRES_PASSWORD / NEXUS_APP_DB_PASSWORD / NEXUS_SECRET_KEY if left CHANGE_ME
   ```
   Fill in: `DOMAIN`, `ACME_EMAIL`, and **freshly rotated** `NEXUS_GROQ_API_KEY(S)` + `NEXUS_EXA_API_KEY(S)`.
   The security flags (rate limit, idempotency, body cap, metrics) are already ON in the template.
3. **Deploy:**
   ```bash
   ./deploy.sh            # builds the image, runs migrations + RLS, starts app+worker+Caddy(TLS)
   ```
4. **Backups (do this on day one):**
   ```bash
   crontab -e
   15 2 * * * cd /opt/nexus-gtm && BACKUP_OFFSITE="s3://YOUR-BUCKET/nexus" scripts/backup_cron.sh >> /var/log/nexus-backup.log 2>&1
   ```
5. **Alerting:** uncomment the `slack_configs` block in `deploy/monitoring/alertmanager.yml`, set
   `SLACK_WEBHOOK_URL`, and bring up the observability overlay (or run Prometheus/Grafana on a
   separate monitoring host). Verify a test alert reaches Slack.
6. **Smoke test:** sign up, seed a demo account, run a pipeline, confirm the SPA loads over HTTPS.

## Must-do before real users (owner actions — cannot be automated)

- [ ] **Rotate the Groq/Exa keys** (they were exposed in chat). Runbook: `docs/runbooks/secret-rotation.md`.
- [ ] Confirm the nightly backup ran and a dump landed offsite (check the log + the bucket).
- [ ] Wire and test the Slack/PagerDuty alert receiver.
- [ ] Run one restore rehearsal against staging (`scripts/dr_rehearsal.sh`).

## Known limitations at this scale (accepted, documented)

- **Single Postgres / single Valkey / single VM.** No HA. A host failure = restore-from-backup
  downtime (RTO ~minutes). Acceptable at 100–250 users; revisit before ~thousands concurrent.
- **In-process rate limiter is per-uvicorn-worker** (2 workers → effective 2× the per-worker limit).
  Caddy edge rate limiting + a Valkey-shared limiter are the scale-up path; the current layer is
  verified-working brute-force defense for the pilot.
- **No blue-green** — a deploy causes ~19 s downtime; run deploys off-hours.
- **No distributed tracing** — request-id logs + Prometheus metrics only.

## Scale-up path (when you approach thousands of users)

1. Managed HA Postgres (RDS/Cloud SQL) + PgBouncer + read replica.
2. ≥2 app replicas behind a real load balancer (app is stateless — safe); worker is leader-elected (C-1) so scaling it is safe.
3. Valkey-backed shared rate limiter; Caddy `rate_limit` plugin at the edge.
4. Blue-green deploys; OpenTelemetry tracing.
5. Re-run `deploy/loadtest` against the sized target for a real P99, and a 24 h soak.

The `deploy/cloud/{aws,azure}` Terraform already targets this managed topology.
