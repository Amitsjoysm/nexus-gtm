# Infojoy GTM — Production Deployment (AWS primary, Azure secondary)

This is the production runbook + architecture for deploying Infojoy GTM to the cloud with a
**single script**. Compute runs on **managed containers (AWS ECS Fargate)**; Postgres + Valkey are
**self-hosted** as Fargate services on persistent EFS storage (cost-conscious choice — see the
reliability note). Everything is Infrastructure-as-Code (Terraform) and CI/CD'd (GitHub Actions).

> One command: `deploy/cloud/deploy.sh aws <domain>` → provisions VPC, ECS, ALB+TLS, EFS, secrets,
> logs, alarms, pushes the image, and brings the stack up. Azure (Container Apps) is the documented
> secondary target (`deploy/cloud/azure/`).

---

## 1. Infrastructure architecture

```
                          Internet
                             │  HTTPS :443
                   ┌─────────▼──────────┐
                   │   Route 53 (DNS)   │  app.<domain>
                   └─────────┬──────────┘
                   ┌─────────▼──────────┐   ACM cert (auto-renew)
                   │  Application LB     │   WAF (optional)
                   │  :443 → target:8000 │   health check: /ready
                   └─────────┬──────────┘
            ┌────────────────┼───────────────────────────┐  VPC 10.0.0.0/16
            │      PUBLIC subnets (a,b): ALB + NAT GW     │
            │  ┌──────────────────────────────────────┐  │
            │  │        PRIVATE subnets (a,b)          │  │
            │  │  ECS Fargate cluster (Service Connect)│  │
            │  │                                       │  │
            │  │  [app]  x2..N   uvicorn :8000  ◄─HPA  │  │  ← autoscaling on CPU/req
            │  │  [worker] x1..N  queue + heartbeat    │  │
            │  │  [postgres] x1   :5432  ─┐            │  │
            │  │  [valkey]   x1   :6379  ─┤ EFS volumes│  │  ← persistent (pgdata, valkey)
            │  └──────────────────────────┴───────────┘  │
            └─────────────────────────────────────────────┘
   ECR (image)   SSM Param Store (secrets)   CloudWatch (logs+metrics+alarms)
```

**Components**

| Concern | AWS | Azure (secondary) |
|---|---|---|
| Compute (app, worker) | ECS Fargate services | Azure Container Apps |
| Inter-service DNS | ECS Service Connect / Cloud Map | ACA internal ingress |
| Ingress + TLS | ALB + ACM + Route 53 | ACA ingress + managed cert |
| Container registry | ECR | ACR |
| Stateful data | Postgres + Valkey on Fargate + **EFS** | ACA + Azure Files |
| Secrets | SSM Parameter Store (SecureString, KMS) | Key Vault |
| Logs / metrics / alarms | CloudWatch Logs + Container Insights + Alarms | Log Analytics + Azure Monitor |
| IaC | Terraform `deploy/cloud/aws` | Terraform `deploy/cloud/azure` |

**Why this shape:** the app is already a clean app+worker+postgres+valkey topology with a
self-migrating entrypoint, `/health` (liveness) and `/ready` (DB readiness) probes, structured
JSON access logs, and opt-in Prometheus `/metrics`. Fargate maps onto it 1:1 with no servers to
patch, and the same container image runs both the API (`uvicorn`) and the worker
(`python -m nexus.workers.worker`).

---

## 2. Deployment workflow

```
 dev → PR ──CI(test+build)──► main ──tag vX.Y.Z──► CD ──► ECR push ──► ECS rolling deploy
                                                              │
                              terraform apply (infra) ────────┘ (first time / infra changes)
```

1. **First deploy (infra):** `deploy/cloud/deploy.sh aws app.example.com` →
   `terraform init && apply` provisions everything, then builds + pushes the image and starts the
   services. The app task's entrypoint runs `bootstrap_db.py` (migrate to head) + `apply_rls.py`
   (least-priv role + RLS) **before** serving — the worker waits for the app to be healthy.
2. **Ongoing app deploys:** push a git tag → GitHub Actions builds the image, pushes to ECR, and
   triggers an ECS rolling update (`force-new-deployment`). Zero-downtime via ALB connection
   draining + ECS `minimumHealthyPercent=100`.
3. **Rollback:** ECS keeps prior task-def revisions; `aws ecs update-service --task-definition <prev>`
   (or re-run CD on the previous tag). DB migrations are additive/backward-compatible by policy.

---

## 3. CI/CD pipeline (GitHub Actions) — with quality gates

**`.github/workflows/ci.yml`** (every PR + push to main) — each is a **blocking gate**:
| Gate | What it enforces |
|---|---|
| **lint** | `ruff check` (F = real bugs: unused imports, undefined names, f-string errors; E9 = syntax). Config in `pyproject.toml`. |
| **typecheck** | frontend `tsc --noEmit` (strict TypeScript). |
| **test** | `pytest -n auto --timeout=120` **+ coverage threshold** (`--cov-fail-under=60`, ratchet up over time). Hermetic/offline. |
| **frontend** | `npm ci && npm run build` (`tsc -b` + vite). |
| **secrets** | **gitleaks** secret scan over full history — no credentials in the repo. |
| **image** | `docker build` + **Trivy** container scan (fails on fixable HIGH/CRITICAL CVEs). |
| **quality-gate** | aggregate job that `needs` all the above — set this as the **single required status check** in branch protection. |
| deps-audit | `pip-audit` + `npm audit` (advisory / non-blocking — promote to a gate once clean). |

**`.github/workflows/cd.yml`** (on `v*` tag or manual dispatch):
- `environment: production` → optional **required-reviewer approval gate**.
- OIDC-assume an AWS role (no long-lived keys), build + push to ECR (SHA + version tags), then
  `aws ecs update-service --force-new-deployment` for app + worker, **wait for `services-stable`**,
  and run a **post-deploy smoke test** (`/health` + `/ready` must return 200) — the release fails if
  the live app isn't healthy. Previous task-def revision stays available for one-command rollback.

Repo settings: secret `AWS_DEPLOY_ROLE_ARN`; variables `AWS_REGION`, `ECR_REPOSITORY`, `ECS_CLUSTER`,
`APP_URL`. In **Branch protection**, require the `quality-gate` check before merge.

---

## 4. Container / Fargate setup

- **One image, two commands.** `deploy/Dockerfile` (multi-stage: Node builds the SPA → slim Python
  runtime) is reused for app (`uvicorn …`) and worker (`python -m nexus.workers.worker`). Non-root
  user, healthcheck baked in.
- **Migrations run once, in the app task** (`NEXUS_RUN_MIGRATIONS=1`); the worker waits on the
  app's health so they never race.
- **Resource sizing (starting point):** app 512 CPU / 1024 MiB ×2; worker 256/512 ×1; postgres
  1024/2048 ×1; valkey 256/512 ×1. Tune from CloudWatch.
- **Kubernetes alternative:** the same image + env contract ports cleanly to EKS/AKS — Deployments
  for app/worker + StatefulSets for postgres/valkey + an Ingress + HPA. ECS Fargate was chosen for
  lower ops; the K8s manifest set is a follow-up if you outgrow Fargate's primitives.

---

## 5. Monitoring & logging

- **Logs:** every container ships stdout/stderr to **CloudWatch Logs** (`/ecs/nexus/<service>`),
  30-day retention. The app emits one structured JSON access line per request (method, path,
  status, duration, request-id) via `RequestContextMiddleware`.
- **Metrics:** ECS **Container Insights** (CPU/mem/network per service). Turn on app Prometheus
  metrics with `NEXUS_METRICS_ENABLED=true` to scrape `/metrics` (only behind the VPC).
- **Health probes:** ALB target group health check hits `/ready` (verifies DB reachability), not
  just `/health`, so an instance with a broken DB connection is pulled from rotation.
- **Alarms (CloudWatch → SNS email/Slack):** ALB 5xx rate, target unhealthy-host count, app/worker
  CPU & memory > 85% for 5 min, ECS running-task-count < desired, and a synthetic `/ready` canary.
- **Tracing (optional next step):** add AWS X-Ray or OTel sidecar; the request-id is already
  propagated for correlation.

---

## 6. Reliability & downtime reduction

- **Zero-downtime deploys:** ALB + ECS rolling update (`minimumHealthyPercent=100`,
  `maximumPercent=200`), connection draining 30s, health-gated.
- **Multi-AZ:** ALB + Fargate tasks spread across two AZs. NAT GW per AZ (or single to save cost
  in non-critical envs).
- **Self-healing:** ECS restarts failed tasks; the worker loop already survives queue outages with
  bounded backoff; the scheduler heartbeat survives enqueue failures.
- **Backups (self-hosted DB):** a scheduled task runs `pg_dump` to S3 nightly (lifecycle to
  Glacier). EFS has automatic backups via AWS Backup. **Test restores quarterly.**
- ⚠️ **The #1 reliability risk is the single self-hosted Postgres.** Fargate+EFS Postgres is a
  single instance with no automatic failover, and EFS (NFS) adds fsync latency under load. For real
  "millions of users" uptime, flip to **managed RDS Multi-AZ + ElastiCache** — it's a localized
  change (swap the two data services for `aws_db_instance` + `aws_elasticache_replication_group` and
  point `NEXUS_DATABASE_URL` / `NEXUS_REDIS_URL` at their endpoints). The Terraform is structured so
  this is the intended upgrade. Until then: nightly backups + tested restore are mandatory.

---

## 7. Scaling

- **App (stateless): horizontal autoscaling** via ECS Application Auto Scaling — target-tracking on
  ALB request-count-per-target (e.g. 1000 req/target) and CPU 60%. Min 2, max 10 (raise as needed).
- **Worker: scale on queue depth** — publish Valkey queue length to a CloudWatch custom metric and
  target-track, or scale on CPU. Workers are idempotent and claim work per-tick, so N>1 is safe.
- **Postgres (vertical):** bump task CPU/mem; add read replicas only after moving to RDS.
- **Per-tenant fairness:** the worker batches per-tenant sweeps; large tenants can't starve the loop.
- **CDN (optional):** front the ALB with CloudFront for the static SPA assets to cut latency + cost.

---

## 8. Production deployment checklist

**Before first deploy**
- [ ] Rotate every secret that was ever shared in chat/CI logs (Gmail app password, API keys).
- [ ] Set a strong `NEXUS_SECRET_KEY` (config rejects the insecure default when `NEXUS_ENV=prod`).
- [ ] Populate `deploy/.env` (gitignored) with all secrets; the deploy script pushes them to SSM.
- [ ] Use a dedicated **infojoy.com** SMTP mailbox (not a personal Gmail) for OTP/reset email.
- [ ] Point `NEXUS_APP_BASE_URL` at the real `https://app.<domain>` (reset links).
- [ ] Confirm `NEXUS_OTP_REGISTRATION_ENABLED=true` and `NEXUS_AUTH_RATE_LIMIT_ENABLED=true`.
- [ ] Remote Terraform state in S3 + DynamoDB lock, **encrypted (KMS)** — secrets land in state.

**Infra**
- [ ] `terraform validate && terraform plan` reviewed before `apply`.
- [ ] ACM cert validated (DNS) for `app.<domain>`; Route 53 A/ALIAS → ALB.
- [ ] Security groups: ALB open 443 from 0.0.0.0/0; app 8000 only from ALB; data 5432/6379 only
      from app/worker; EFS 2049 only from data tasks.
- [ ] EFS access points have correct POSIX uid/gid for the postgres image.

**App**
- [ ] CI green (suite + frontend build) on the deployed SHA.
- [ ] Migrations are additive/backward-compatible (verified: single linear head).
- [ ] `/health` 200, `/ready` 200, `/api/auth/forgot-password` 202, `/api/auth/signup` 403 (OTP).

**Observability & DR**
- [ ] CloudWatch alarms wired to SNS (email/Slack); test one.
- [ ] Nightly `pg_dump` → S3 verified; **perform one test restore**.
- [ ] Log retention + cost reviewed.

**Post-deploy smoke test**
- [ ] Register a workspace end-to-end (OTP email received), log in, create an account, run a pipeline.
- [ ] Trigger a forced ECS deploy and confirm zero dropped requests (ALB 5xx stays flat).

---

## 9. Single-script usage

```bash
# AWS (primary) — provisions everything + deploys:
deploy/cloud/deploy.sh aws app.example.com

# Azure (secondary) — see deploy/cloud/azure/README.md
deploy/cloud/deploy.sh azure app.example.com

# Tear down (careful — destroys infra; data volumes/backups per retention policy):
deploy/cloud/deploy.sh aws app.example.com destroy
```

Prereqs: `aws` CLI authenticated, `terraform` ≥ 1.6, `docker`. The script reads `deploy/.env` for
secrets, runs Terraform, builds + pushes the image to ECR, and forces an ECS deploy.
