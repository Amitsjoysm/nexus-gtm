# Cloud deployment (single script)

Production infra for Infojoy GTM as Terraform + a one-command wrapper. Full design, monitoring,
reliability, scaling, and the production checklist live in [`docs/deploy/PRODUCTION.md`](../../docs/deploy/PRODUCTION.md).

```
deploy/cloud/
  deploy.sh          # single entrypoint: ./deploy.sh aws <domain> [destroy]
  aws/               # AWS ECS Fargate stack (PRIMARY, complete)
  azure/             # Azure Container Apps (SECONDARY, scaffolded — see azure/README.md)
```

## Quick start (AWS)

Prereqs: `terraform` ≥ 1.6, `docker`, `aws` CLI authenticated, a Route 53 hosted zone for your domain,
and a filled-in `deploy/.env` (secrets; gitignored).

```bash
# from the repo root
deploy/cloud/deploy.sh aws app.example.com
```

What it does: composes the in-cluster DB/Redis URLs from `deploy/.env`, `terraform apply`s the VPC +
ECS + ALB(+TLS) + EFS + secrets + logs + alarms, builds and pushes the image to ECR, and rolls out
the app + worker. First run prints the URL once DNS + ACM validate (a few minutes).

**Before you run it:** review the [production checklist](../../docs/deploy/PRODUCTION.md#8-production-deployment-checklist) —
especially: rotate any shared secrets, set a strong `NEXUS_SECRET_KEY`, point `NEXUS_APP_BASE_URL`
at the real host, and configure remote **encrypted** Terraform state (secrets land in state).

## Validate before applying

Terraform isn't installed in this dev box, so validate in your environment first:

```bash
cd deploy/cloud/aws
terraform init
terraform fmt -check
terraform validate
terraform plan -var domain=app.example.com -var route53_zone_name=example.com
```

## Day-2 deploys

After the first `terraform apply`, ship new code via CI/CD — tag a release and GitHub Actions
(`.github/workflows/cd.yml`) builds, pushes to ECR, and forces an ECS rolling update. No Terraform
needed for app-only changes; re-run `deploy.sh` only when infra changes.

## Upgrading to managed data (recommended at scale)

Self-hosted Postgres/Valkey on Fargate+EFS is a single point of failure. To move to managed
**RDS Multi-AZ + ElastiCache**: add `aws_db_instance` + `aws_elasticache_replication_group`, drop the
postgres/valkey ECS services, and point `NEXUS_DATABASE_URL` / `NEXUS_REDIS_URL` at their endpoints
(the secret composition in `deploy.sh` is the only other change). See the reliability note in the
design doc.
