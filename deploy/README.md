# NEXUS GTM — Production Deployment (single VM)

One VM, one script. The stack: **Caddy** (automatic HTTPS) → **FastAPI app** (API + built
SPA, 2 workers) + **worker** (queue consumer + automation heartbeat) → **Postgres 16**
(data) + **Valkey 8** (task queue, Redis-protocol).

## Deploy in three steps

```bash
# 1. Point DNS: an A record for your domain (e.g. app.autosdr.ai) -> the VM's IP.

# 2. On the VM (Ubuntu 22.04+ or any Docker host):
git clone <your-repo> nexus && cd nexus/deploy        # or unzip the release artifact

# 3. One script — builds, migrates, starts, health-checks:
./deploy.sh app.autosdr.ai
```

That's it. Caddy obtains the TLS certificate automatically. To host on a different
domain, run `./deploy.sh other.domain.com` — nothing else changes.

## What deploy.sh does

1. Creates `deploy/.env` from the example on first run and **generates** the Postgres
   password and JWT secret (the app refuses to boot in `prod` with the insecure default).
2. Builds the production image (multi-stage: Node builds the SPA, Python serves it).
3. Starts Postgres + Valkey, waits for health, runs `alembic upgrade head`.
4. Starts app, worker, and Caddy; gates on the app's `/ready` (DB reachable) check.

## Operations

| Task | Command (from `deploy/`) |
|---|---|
| Logs | `docker compose -f docker-compose.prod.yml logs -f app worker` |
| Update to a new version | `git pull && ./deploy.sh` (rebuild + migrate + restart) |
| DB backup | `docker compose -f docker-compose.prod.yml exec postgres pg_dump -U nexus nexus > backup-$(date +%F).sql` |
| DB restore | `cat backup.sql \| docker compose -f docker-compose.prod.yml exec -T postgres psql -U nexus nexus` |
| Stop everything | `docker compose -f docker-compose.prod.yml down` |
| Wipe and start over | `docker compose -f docker-compose.prod.yml down -v` (DESTROYS DATA) |

## Configuration

All app settings are `NEXUS_*` env vars in `deploy/.env` (see `.env.production.example`).
Notables:

- `NEXUS_LLM_PROVIDER=stub` runs the entire product loop deterministically with **no API
  key** — fine for a pilot; switch to a real provider for production agent output.
- `NEXUS_AUTOMATION_ENABLED=true` turns on the heartbeat (account refresh, cadence
  advance, daily digests). Each tenant still opts in via Settings → Continuous automation.
- `NEXUS_CRM_SYNC_ENABLED=true` + `NEXUS_CRM_PROVIDER=salesforce|hubspot` enables CRM
  auto-sync once a real connector is configured.

## Sizing

A 2 vCPU / 4 GB VM comfortably runs a multi-team pilot. The app and worker scale
independently (`docker compose up -d --scale worker=3`); the Valkey-backed queue makes
extra workers safe. Beyond one VM: move Postgres/Valkey to managed services (set
`NEXUS_DATABASE_URL` / `NEXUS_REDIS_URL` in `.env`) and run app/worker on multiple hosts
behind any load balancer.

## Release artifact

`scripts/package_release.sh` builds `release/nexus-gtm-<version>.zip` — the exact tracked
source tree (no secrets, no build junk). Unzip on the VM and run `deploy/deploy.sh` for an
air-gapped or registry-free deployment.
