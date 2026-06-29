# Terraform owns the composed connection URLs because it (and only it) knows the endpoints —
# the in-cluster DNS names for self-hosted, or the RDS/ElastiCache endpoints for managed. The
# deploy script supplies only the raw passwords + API keys (var.secrets); the URLs are derived
# here and stored as their own SSM SecureString params, referenced by the task definitions.
locals {
  pg_password  = lookup(var.secrets, "POSTGRES_PASSWORD", "")
  app_password = lookup(var.secrets, "NEXUS_APP_DB_PASSWORD", "")

  # splat+join avoids "index out of range" on the count=0 (self-hosted) side of the conditional.
  managed_pg_host    = join("", aws_db_instance.main[*].address)
  managed_redis_host = join("", aws_elasticache_replication_group.main[*].primary_endpoint_address)

  pg_host    = var.use_managed_data ? local.managed_pg_host : "postgres.${var.project}.local"
  redis_host = var.use_managed_data ? local.managed_redis_host : "valkey.${var.project}.local"

  # The API connects as the least-priv RLS role (nexus_app); the worker + migrations as the owner
  # (nexus). apply_rls.py (run in the app entrypoint) creates nexus_app with NEXUS_APP_DB_PASSWORD.
  conn_urls = {
    NEXUS_DATABASE_URL        = "postgresql+asyncpg://nexus_app:${local.app_password}@${local.pg_host}:5432/nexus"
    NEXUS_DB_OWNER_URL        = "postgresql+asyncpg://nexus:${local.pg_password}@${local.pg_host}:5432/nexus"
    NEXUS_WORKER_DATABASE_URL = "postgresql+asyncpg://nexus:${local.pg_password}@${local.pg_host}:5432/nexus"
    NEXUS_REDIS_URL           = "redis://${local.redis_host}:6379/0"
  }
}

resource "aws_ssm_parameter" "conn" {
  for_each = local.conn_urls
  name     = "/${var.project}/${var.env}/${each.key}"
  type     = "SecureString"
  value    = each.value
  tags     = { Name = each.key }
}
